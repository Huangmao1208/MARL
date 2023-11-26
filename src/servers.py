import os
import sys

import numpy as np


class Server:
    def __init__(self, server_id, policy, app, server_config, utility_normalization_factor):
        self.server_id = server_id

        self.server_state = 0       # initial state: Active
        self.action = 1             # initial action: Not sprint
        self.reward = 0             # initial reward: Zero
        self.utility_normalization_factor = utility_normalization_factor

        self.frac_sprinters = 0

        self.policy = policy
        self.app = app

        self.cooling_prob = server_config["cooling_prob"]
        self.change = server_config["change"]
        self.change_iteration = server_config["change_iteration"]
        self.change_type = server_config["change_type"]

        self.reward_history = []

    def get_action_utility(self, threshold):
        if self.server_state == 0 and self.app.get_current_state() >= threshold:
            return 0, self.app.get_sprinting_utility()
        else:
            return 1, self.app.get_nominal_utility()

    # update application state, rack_state, server_state, and fractional number of sprinters.
    def update_state(self, cost, frac_sprinters):
        self.app.update_state(self.action)
        self.reward -= cost
        self.frac_sprinters = frac_sprinters

        if self.server_state == 1:
            assert self.action == 1
            if np.random.rand() > self.cooling_prob:    # stay in cooling
                self.server_state = 0
        elif self.action == 0:     # go to cooling
            self.server_state = 1

        self.reward_history.append(self.reward)

    def update_policy(self):
        pass

    def take_action(self):
        pass

    def run_server(self, cost, frac_sprinters, iteration):
        if self.change == 1 and iteration == self.change_iteration:
            if self.change_type == 0:
                self.app.arrival_tps *= 1.25
            elif self.change_type == 1:
                self.app.arrival_tps *= 0.8
            elif self.change_type == 2:
                self.cooling_prob = 1 - (1 - self.cooling_prob)/2
            elif self.change_type == 3:
                self.cooling_prob /= 2

        self.update_state(cost, frac_sprinters)
        self.update_policy()
        self.take_action()
        return self.action

    # write reward into files
    def print_rewards_and_app_states(self, path):
        file_path = os.path.join(path, f"server_{self.server_id}_rewards.txt")
        with open(file_path, 'w+') as file:
            for r in self.reward_history:
                file.write(f"{str(r)}\n")
        self.app.print_state(self.server_id, path)


# Server with Actor-Critic policy
class ACServer(Server):
    def __init__(self, server_id, policy, app, server_config, state_normalization_factor, utility_normalization_factor):
        super().__init__(server_id, policy, app, server_config, utility_normalization_factor)
        self.state_normalization_factor = state_normalization_factor
        self.update_actor = 0

    # Update Actor and Critic networks' parameters
    def update_policy(self):
        new_state = self.state_normalization_factor * np.array([self.server_state,
                                                                self.app.get_current_state(),
                                                                self.frac_sprinters])
        self.policy.update_policy(new_state, self.reward, self.update_actor)

    # get threshold value and state value from AC_Policy network, choose sprint or not and get immediate reward
    def take_action(self):
        self.update_actor = 1 - self.server_state
        threshold = 1
        if self.update_actor:
            state = self.state_normalization_factor * np.array([self.frac_sprinters])
            threshold = self.policy.get_new_action(state)
        self.action, self.reward = self.get_action_utility(threshold)
        self.reward *= self.utility_normalization_factor


#  Server with threshold policy.
#  It is a fixed policy, so it doesn't need update policy
class ThrServer(Server):
    def __init__(self, server_id, policy, app, server_config, utility_normalization_factor):
        super().__init__(server_id, policy, app, server_config, utility_normalization_factor)

    def update_policy(self):
        return

    # Get sprinting probability from Thr_Policy, and choose sprint or not by this probability, and get immediate reward
    def take_action(self):
        action = self.policy.get_new_action(0)
        self.action, self.reward = self.get_action_utility(action)
        self.reward *= self.utility_normalization_factor

