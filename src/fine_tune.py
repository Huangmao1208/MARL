import optuna
import json
import subprocess


def format(obj, indent_level=0):
    INDENT_SIZE = 4

    # Function to determine if an object is a matrix
    def is_matrix(array):
        return isinstance(array, list) and all(isinstance(row, list) for row in array)

    # Format matrix
    def format_matrix(matrix, indent_level):
        rows = []
        indent = ' ' * INDENT_SIZE * indent_level
        for row in matrix:
            formatted_row = ', '.join(map(str, row))
            rows.append(indent + ' ' * INDENT_SIZE + '[' + formatted_row + ']')
        return '[\n' + ',\n'.join(rows) + '\n' + indent + ']'

    if is_matrix(obj):
        return format_matrix(obj, indent_level)

    elif isinstance(obj, list):
        return '[' + ', '.join(json.dumps(item) for item in obj) + ']'

    elif isinstance(obj, dict):
        items = []
        indent = ' ' * INDENT_SIZE * indent_level
        for key, value in obj.items():
            formatted_value = format(value, indent_level + 1)
            items.append(f'"{key}": {formatted_value}')
        return '{\n' + ',\n'.join(indent + ' ' * INDENT_SIZE + item for item in items) + '\n' + indent + '}'

    else:
        return json.dumps(obj)


def objective(trial, config_file_name, app_type_id, app_type_sub_id, policy_id):
    # Load the configuration file
    with open(config_file_name, 'r') as f:
        config = json.load(f)
    app_type = config["app_types"][app_type_id]
    app_sub_type = config["app_sub_types"][app_type][app_type_sub_id]
    a = trial.suggest_int("a", 2, 10, step=2)
    b = trial.suggest_int("b", 2, 10, step=2)
    config["ac_discount_factor"][app_type][app_sub_type] = trial.suggest_categorical("ac_discount_factor",
                                                                                     [0.99, 0.999, 0.9999])
    add_noise = config["coordinator_config"]["add_noise"]
    if add_noise:
        config["std_max_noise"][app_type][app_sub_type] = trial.suggest_float("std_max", 0.01, 0.1, step=0.01)
        config["c_lr_noise"][app_type][app_sub_type] = trial.suggest_categorical("c_lr_noise", [0.001, 0.002, 0.003,
                                                                                                0.004, 0.005, 0.006,
                                                                                                0.007, 0.008, 0.009])
        config['a_lr_noise'][app_type][app_sub_type] = config["c_lr_noise"][app_type][app_sub_type] / b
        config["sprinters_decay_factor_noise"][app_type][app_sub_type] = 1 - config["a_lr_noise"][app_type][
            app_sub_type] * a
        config["state_normalization_factor_noise"][app_type][app_sub_type] = trial.suggest_float(
            "state_normalization_factor_noise", 0.01, 0.1, step=0.01)
    else:
        config["std_max_no_noise"][app_type][app_sub_type] = trial.suggest_float("std_max", 0.01, 0.1, step=0.01)
        config["c_lr_no_noise"][app_type][app_sub_type] = trial.suggest_categorical("c_lr_no_noise",
                                                                                    [0.001, 0.002, 0.003,
                                                                                     0.004, 0.005, 0.006,
                                                                                     0.007, 0.008, 0.009])
        config['a_lr_no_noise'][app_type][app_sub_type] = config["c_lr_no_noise"][app_type][app_sub_type] / b
        config["sprinters_decay_factor_no_noise"][app_type][app_sub_type] = 1 - config["a_lr_no_noise"][app_type][
            app_sub_type] * a
        config["state_normalization_factor_no_noise"][app_type][app_sub_type] = trial.suggest_float(
            "state_normalization_factor_no_noise", 0.01, 0.1, step=0.01)
    # Update the parameters in the config

    with open(config_file_name, 'w') as f:
        f.write(format(config))

    subprocess.run(
        ['python3', '~/Documents/Project/MARL/src/multiprocessing_MARL.py', str(app_type_id), str(app_type_sub_id),
         str(policy_id)], check=True)
    result = subprocess.run(
        ['python3', '~/Documents/Project/MARL/src/plot_images.py', str(app_type_id), str(app_type_sub_id),
         str(policy_id)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    # Extract the average reward from the output
    for line in result.stdout.decode().split('\n'):
        avg_reward = float(line)
        break
    else:
        raise RuntimeError('Average reward not found in output')

    return avg_reward


# Load the existing configuration file

config_file_name = '~/Documents/Project/MARL/configs/config.json'
with open(config_file_name, 'r') as f:
    config = json.load(f)

app_type_id = 3
app_type_sub_ids = [0, 1, 2, 3, 4]
policy_id = 0
add_noise = config["coordinator_config"]["add_noise"]
app_type = config["app_types"][app_type_id]
for app_type_sub_id in app_type_sub_ids:
    app_sub_type = config["app_sub_types"][app_type][app_type_sub_id]
    policy_type = config["policy_types"][policy_id]
    study = optuna.create_study(direction='maximize')
    study.optimize(lambda trial: objective(trial, config_file_name, app_type_id, app_type_sub_id, policy_id),
                   n_trials=50)
    print(study.best_params)
    # Get the best parameters
    best_params = study.best_params
    # Update the parameters in the config
    if add_noise:
        config["c_lr_noise"][app_type][app_sub_type] = best_params["c_lr_noise"]
        config['a_lr_noise'][app_type][app_sub_type] = config["c_lr_noise"][app_type][app_sub_type] / best_params["b"]
        config['sprinters_decay_factor_noise'][app_type][app_sub_type] = 1 - config["a_lr_noise"][app_type][
            app_sub_type] * best_params["a"]
        config["state_normalization_factor_noise"][app_type][app_sub_type] = best_params[
            "state_normalization_factor_noise"]
        config["std_max_noise"][app_type][app_sub_type] = best_params["std_max"]
    else:
        config["c_lr_no_noise"][app_type][app_sub_type] = best_params["c_lr_no_noise"]
        config['a_lr_no_noise'][app_type][app_sub_type] = config["c_lr_no_noise"][app_type][app_sub_type] / best_params[
            "b"]
        config['sprinters_decay_factor_no_noise'][app_type][app_sub_type] = 1 - config["a_lr_no_noise"][app_type][
            app_sub_type] * best_params["a"]
        config["state_normalization_factor_no_noise"][app_type][app_sub_type] = best_params[
            "state_normalization_factor_no_noise"]
        config["std_max_no_noise"][app_type][app_sub_type] = best_params["std_max"]
    config["ac_discount_factor"][app_type][app_sub_type] = best_params["ac_discount_factor"]
    with open("configs/config.json", 'w') as f:
        f.write(format(config))
