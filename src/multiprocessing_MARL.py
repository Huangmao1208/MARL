import sys
from multiprocessing import Process, Queue
import numpy as np
import torch
import time
import os
import json

import applications
import policies
import servers

import argparse


def set_seed(seed):
    torch.manual_seed(seed)
    np.random.seed(seed)


#set_seed(42)

"""
Coordinator: Communicates with workers and aggregates servers actions to determine if circuit breaker trips.
"""


class Coordinator:
    def __init__(self, coordinator_config, w2c_queues, c2w_queues, num_workers, num_servers, sprinters_decay_factor, var):

        # Sprinters parameters
        self.frac_sprinters = 0  # Initialize num_sprinting
        self.avg_frac_sprinters_corrected = 0
        self.avg_frac_sprinters = 0  # initial value for exponential moving average of num_sprinting
        self.sprinters_decay_factor = sprinters_decay_factor  # for fictitious play
        self.avg_frac_sprinters_list = []

        self.itr = 0
        self.period = coordinator_config["period"]
        self.fr = 0
        self.cst = np.zeros(num_servers)

        # Iteration parameters
        self.total_iterations_dp = coordinator_config["total_iterations"]
        self.total_iterations = self.total_iterations_dp * self.period
        self.current_iteration = 0  # store total number of active rounds

        # Worker and server parameters
        self.num_workers = num_workers
        self.w2c_queues = w2c_queues
        self.c2w_queues = c2w_queues
        self.num_servers = num_servers
        self.costs = np.zeros(self.num_servers)

        # Recovery parameters
        self.global_cost = coordinator_config["global_cost"]
        self.local_cost = coordinator_config["local_cost"]
        self.min_frac = coordinator_config["min_frac"]  # lower bound of sprinters of system trip
        self.max_frac = coordinator_config["max_frac"]  # higher bound of sprinters of system trip

        # Privacy parameters
        if var == -1:
            self.c_epsilon = coordinator_config["c_epsilon"]
            self.c_delta = coordinator_config["c_delta"]
            self.epsilon = self.c_epsilon / np.log10(self.num_servers)
            self.epsilon_prime = self.epsilon / self.total_iterations_dp
            #self.epsilon_prime = self.epsilon / 120
            self.delta = self.c_delta / self.num_servers
            self.alpha = 1 + 2 * np.log10(1 / self.delta) / self.epsilon
            self.var = self.alpha / (2 * self.num_servers ** 2 * self.epsilon_prime)
            self.sigma = np.sqrt(self.var)
        else:
            self.var = var
            self.sigma = np.sqrt(self.var)
        self.add_noise = coordinator_config["add_noise"]
        self.count_sprint_epoch = np.zeros(self.num_servers)

    #   Whether system trips or not
    def calculate_costs(self):
        self.costs = self.calculate_local_costs() + self.calculate_global_costs()

    def calculate_global_costs(self):
        global_cost_factor = min(max((self.frac_sprinters - self.min_frac) / (self.max_frac - self.min_frac), 0), 1)
        return self.global_cost * global_cost_factor * np.ones(self.num_servers)

    def calculate_local_costs(self):
        local_cost_factor = (np.tanh(30 * (self.frac_sprinters - self.max_frac)) + 1) / 2
        return self.local_cost * local_cost_factor * self.count_sprint_epoch

    # Calculate number of sprinters in this round, determining whether system trip or not.
    # Calculate the fractional number of sprinters by Bias-Corrected Exponential Weighted Moving Average
    # Add noise on the fraction number of sprinters in this round (# of sprinters / total # of servers)
    def aggregate_actions(self, actions):
        self.count_sprint_epoch[np.where(actions == 0)] += 1
        self.frac_sprinters = (self.num_servers - actions.sum()) / self.num_servers
        if self.add_noise == 1:
            self.frac_sprinters += np.random.normal(loc=0, scale=self.sigma)

        self.current_iteration += 1
        self.avg_frac_sprinters *= self.sprinters_decay_factor
        self.avg_frac_sprinters += (1 - self.sprinters_decay_factor) * self.frac_sprinters
        self.avg_frac_sprinters_corrected = self.avg_frac_sprinters / (
                1 - self.sprinters_decay_factor ** self.current_iteration)

    # Main function for coordinator
    def run_coordinator(self, path):
        actions_array = np.zeros(self.num_servers)
        server_ids = np.arange(0, self.num_servers)
        workers_server_ids = np.array_split(server_ids, self.num_workers)

        while self.current_iteration < self.total_iterations:
            # Split the array into self.num_workers parts
            # workers_costs = np.array_split(self.costs, self.num_workers)
            workers_costs = np.array_split(self.cst, self.num_workers)

            # Now, iterate over the queues and reshaped states
            for q, costs in zip(self.c2w_queues, workers_costs):
                # q.put((self.avg_frac_sprinters_corrected, costs, self.current_iteration))
                q.put((self.fr, costs, self.current_iteration))

            # get information from workers
            for q, ids in zip(self.w2c_queues, workers_server_ids):
                actions = q.get()
                actions_array[ids] = actions

            self.aggregate_actions(actions_array)
            self.calculate_costs()
            self.avg_frac_sprinters_list.append(self.avg_frac_sprinters_corrected)

            self.itr += 1
            if self.itr == self.period:
                self.fr = self.avg_frac_sprinters_corrected
                self.cst = self.costs
                self.itr = 0
                self.count_sprint_epoch = np.zeros(self.num_servers)

        # Send stop to all
        for q in self.c2w_queues:
            q.put('stop')

        self.print_frac_sprinters(path)

    # Record fractional number of sprinters in each iteration
    def print_frac_sprinters(self, path):
        file_path = os.path.join(path, "frac_sprinters.txt")
        with open(file_path, 'w+') as file:
            for fs in self.avg_frac_sprinters_list:
                # fs_num = round(np.mean(fs.tolist()), 2)
                file.write(f"{fs}\n")


"""
Workers: Manages several servers
"""


class Worker:
    def __init__(self, servers_list, w2c_queue, c2w_queue):
        self.num_servers = len(servers_list)
        self.servers_list = servers_list
        self.w2c_queue = w2c_queue
        self.c2w_queue = c2w_queue

    def run_worker(self, path):
        while True:
            actions = np.ones(self.num_servers)
            # Get info from coordinator
            info = self.c2w_queue.get()
            if info == 'stop':
                for server in self.servers_list:
                    server.print_rewards_and_app_states(path)
                break

            frac_sprinters, costs, iteration = info
            for i, server in enumerate(self.servers_list):
                action = server.run_server(costs[i], frac_sprinters, iteration)
                actions[i] = action
            # Send infor to coordinator
            self.w2c_queue.put(actions)


def main(config_file_name, app_type_id, app_sub_type_id, policy_id, threshold_in):
    start_time = time.time()
    with open(config_file_name, 'r') as f:
        config = json.load(f)
    folder_name = config["folder_name"]
    coordinator_config = config["coordinator_config"]
    servers_config = config["servers_config"]
    num_workers = config["num_workers"]
    num_servers = config["num_servers"]
    app_type = config["app_types"][app_type_id]
    assert app_sub_type_id < len(config["app_sub_types"][app_type])
    app_sub_type = config["app_sub_types"][app_type][app_sub_type_id]
    policy_type = config["policy_types"][policy_id]
    app_utilities = config["app_utilities"]
    add_noise = coordinator_config["add_noise"]
    add_change = servers_config["change"]
    period = coordinator_config["period"]
    utility_normalization_factor = config["utility_normalization_factor"][app_type][app_sub_type]
    var = coordinator_config["var"]
    if add_noise:
        sprinters_decay_factor = config["sprinters_decay_factor_noise"][app_type][app_sub_type]
    else:
        sprinters_decay_factor = config["sprinters_decay_factor_no_noise"][app_type][app_sub_type]

    path = f"{folder_name}/{num_servers}_server/{policy_type}/{app_type}_{app_sub_type}"
    if not os.path.exists(path):
        os.makedirs(path)

    w2c_queues = [Queue() for _ in range(num_workers)]
    c2w_queues = [Queue() for _ in range(num_workers)]

    servers_list = []
    worker_processors = []

    coordinator = Coordinator(coordinator_config, w2c_queues, c2w_queues, num_workers, num_servers,
                              sprinters_decay_factor, var)

    for i in range(num_servers):
        if app_type == "markov":
            transition_matrix = config["markov_app_transition_matrices"][app_sub_type]
            app = applications.MarkovApp(transition_matrix, app_utilities, np.random.choice(app_utilities))
        elif app_type == "uniform":
            app = applications.UniformApp(app_utilities)
        elif app_type == "queue":
            if add_change == 1:
                arrival_tps = config["queue_app_arrival_tps_change"][app_sub_type]
            else:
                arrival_tps = config["queue_app_arrival_tps"][app_sub_type]
            sprinting_tps = config["queue_app_sprinting_tps"][app_sub_type]
            nominal_tps = config["queue_app_nominal_tps"][app_sub_type]
            max_queue_length = config["queue_app_max_queue_length"][app_sub_type]
            app = applications.QueueApp(arrival_tps, sprinting_tps, nominal_tps, max_queue_length)
        elif app_type == "spark":
            with open("data/gain.txt") as file:
                if app_sub_type == "s1":
                    for line in file:
                        if "als_gain" in line.strip():
                            gains = line.strip().split(":")[1].split("\t")
                            break
                elif app_sub_type == "s2":
                    for line in file:
                        if "kmeans_gain" in line.strip():
                            gains = line.strip().split(":")[1].split("\t")
                            break
                elif app_sub_type == "s3":
                    for line in file:
                        if "lr_gain" in line.strip():
                            gains = line.strip().split(":")[1].split("\t")
                            break
                elif app_sub_type == "s4":
                    for line in file:
                        if "pr_gain" in line.strip():
                            gains = line.strip().split(":")[1].split("\t")
                            break
                elif app_sub_type == "s5":
                    for line in file:
                        if "svm_gain" in line.strip():
                            gains = line.strip().split(":")[1].split("\t")
                            break
                else:
                    sys.exit("Invalid sub type!")

                gains = np.array(gains).astype(float)
                app = applications.SparkApp(gains, np.random.choice(np.arange(np.array(gains).size)))
        else:
            sys.exit("wrong app type!")

        if policy_type == "ac_policy":
            if add_noise:
                a_lr = config["a_lr_noise"][app_type][app_sub_type]
                c_lr = config["c_lr_noise"][app_type][app_sub_type]
                state_normalization_factor = config["state_normalization_factor_noise"][app_type][app_sub_type]
                std_max = config["std_max_noise"][app_type][app_sub_type]
            else:
                a_lr = config["a_lr_no_noise"][app_type][app_sub_type]
                c_lr = config["c_lr_no_noise"][app_type][app_sub_type]
                state_normalization_factor = config["state_normalization_factor_no_noise"][app_type][app_sub_type]
                std_max = config["std_max_no_noise"][app_type][app_sub_type]
            a_h1_size = config["ac_policy_config"]["a_h1_size"]
            c_h1_size = config["ac_policy_config"]["c_h1_size"]
            df = config["ac_discount_factor"][app_type][app_sub_type]
            mini_batch_size = config["ac_policy_config"]["mini_batch_size"]
            policy = policies.ACPolicy(1, 3, a_h1_size, c_h1_size, a_lr, c_lr, df, std_max, mini_batch_size)
            server = servers.ACServer(i, period, policy, app, servers_config,
                                      state_normalization_factor, utility_normalization_factor)
        elif policy_type == "thr_policy":
            threshold = threshold_in
            if threshold == -1:
                threshold = config["threshold"][app_type][app_sub_type]
            policy = policies.ThrPolicy(threshold)
            server = servers.ThrServer(i, period, policy, app, servers_config, utility_normalization_factor)
        elif policy_type == "dp_policy":
            threshold = threshold_in
            if threshold == -1:
                if add_change == 1:
                    threshold = config["dp_threshold_change"][app_type][app_sub_type]
                else:
                    threshold = config["dp_threshold"][app_type][app_sub_type]
            policy = policies.ThrPolicy(threshold)
            server = servers.ThrServer(i, period, policy, app, servers_config, utility_normalization_factor)
        elif policy_type == "ql_policy":
            dim = (2, app.get_state_space_len())
            epsilon = config["ql_policy_config"]["epsilon"]
            learning_rate = config["ql_lr"][app_type][app_sub_type]
            discount_factor = config["ql_policy_config"]["discount_factor"]
            policy = policies.QLPolicy(dim, discount_factor, learning_rate, epsilon)
            server = servers.QLServer(i, period, policy, app, servers_config, utility_normalization_factor)
        else:
            sys.exit("Wrong policy type!")

        servers_list.append(server)

    ids_list = np.array_split(np.arange(0, num_servers), num_workers)
    for i in range(0, num_workers):
        worker = Worker(servers_list[ids_list[i][0]:ids_list[i][-1] + 1], w2c_queues[i], c2w_queues[i])
        worker_processor = Process(target=worker.run_worker, args=(path,))
        worker_processors.append(worker_processor)
        worker_processor.start()

    coordinator_processor = Process(target=coordinator.run_coordinator, args=(path,))
    coordinator_processor.start()

    for worker_processor in worker_processors:
        worker_processor.join()

    coordinator_processor.join()

    end_time = time.time()
    total_time = end_time - start_time
    print(f"Total running time: {total_time} seconds")


if __name__ == "__main__":
    config_file = "configs/config.json"

    main(config_file, 3, 1, 1, -1)

    """parser = argparse.ArgumentParser()
    parser.add_argument('app_type_id', type=int)
    parser.add_argument('app_type_sub_id', type=int)
    parser.add_argument('policy_id', type=int)
    args = parser.parse_args()
    main(config_file, args.app_type_id, args.app_type_sub_id, args.policy_id, -1)"""