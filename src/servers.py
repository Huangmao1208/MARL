import os
from torch.distributions import Normal
import torch
import numpy as np


class Server:
    def __init__(self, server_id, policy, app, server_config):
        self.server_id = server_id

        self.server_state = 0       # initial state: Active
        self.action = Normal(loc=1, scale=0.1).sample()             # initial action: Not sprint
        self.taken_action = 1
        self.reward = 0             # initial reward: Zero

        self.frac_sprinters = 0

        self.policy = policy
        self.app = app

        self.cooling_prob = server_config["cooling_prob"]
        self.discount_factor = server_config["discount_factor"]

        self.reward_history = []

    def get_action_delta_utility(self):
        if self.server_state == 0 and self.action.item() == 0:
            return 0, self.app.get_delta_utility()
        else:
            return 1, 0

    # update application state, rack_state, server_state, and fractional number of sprinters.
    def update_state(self, cost, frac_sprinters):
        self.app.update_state(self.action.item())
        self.reward -= cost
        self.frac_sprinters = torch.tensor([frac_sprinters], dtype=torch.float32)

        if self.server_state == 1:
            assert self.taken_action == 1
            if np.random.rand() > self.cooling_prob:    # stay in cooling
                self.server_state = 0
        elif self.taken_action == 0:     # go to cooling
            self.server_state = 1

        self.reward_history.append(self.reward)

    def update_policy(self):
        pass

    def take_action(self):
        pass

    def run_server(self, cost, frac_sprinters, iteration):
        self.update_state(cost, frac_sprinters)
        self.update_policy()
        self.take_action()
        return self.taken_action

    # write reward into files
    def print_rewards_and_app_states(self, path):
        file_path = os.path.join(path, f"server_{self.server_id}_rewards.txt")
        with open(file_path, 'w+') as file:
            for r in self.reward_history:
                file.write(f"{str(r)}\n")
        self.app.print_state(self.server_id, path)


# Server with Actor-Critic policy
class ACServer(Server):
    def __init__(self, server_id, policy, app, server_config, normalization_factor):
        super().__init__(server_id, policy, app, server_config)
        self.state_value = torch.tensor([0.0], requires_grad=True)
        self.distribution = Normal(loc=1, scale=0.1)
        self.a_next_state_tensor = None
        self.c_next_state_tensor = None
        self.update_actor = 0
        self.normalization_factor = normalization_factor

    def update_state(self, cost, frac_sprinters):
        super().update_state(cost, frac_sprinters)

        server_state_tensor = torch.tensor([self.server_state], dtype=torch.float32)
        app_delta_utility_tensor = torch.tensor([self.app.get_delta_utility()], dtype=torch.float32)
        frac_sprinters_tensor = torch.tensor([self.frac_sprinters], dtype=torch.float32)

        self.a_next_state_tensor = self.normalization_factor * frac_sprinters_tensor
        self.c_next_state_tensor = self.normalization_factor * torch.cat((server_state_tensor,
                                                                          app_delta_utility_tensor,
                                                                          frac_sprinters_tensor))

    # Update Actor and Critic networks' parameters
    def update_policy(self):
        next_state_value = self.policy.forward_critic(self.c_next_state_tensor)
        estimate = self.reward + self.discount_factor * next_state_value.detach()
        advantage = estimate - self.state_value

        action_log_prob = self.distribution.log_prob(self.action).unsqueeze(0)
        actor_loss = -action_log_prob * advantage.detach()

        critic_loss = advantage.pow(2).mean()

        # Update the actor
        if self.update_actor:
            self.policy.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.policy.actor_optimizer.step()

        # Update the critic
        self.policy.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.policy.critic_optimizer.step()

    # get threshold value and state value from AC_Policy network, choose sprint or not and get immediate reward
    def take_action(self):
        input_tensor = [self.a_next_state_tensor, self.c_next_state_tensor]
        self.distribution, self.state_value = self.policy(input_tensor)
        self.action = self.distribution.sample()
        self.taken_action, self.reward = self.get_action_delta_utility()
        self.update_actor = 1 - self.server_state

    def get_action_delta_utility(self):
        if self.server_state == 0 and self.app.get_delta_utility() > self.action.item():
            return 0, self.app.get_delta_utility()
        else:
            return 1, 0


#  Server with threshold policy.
#  It is a fixed policy, so it doesn't need update policy
class ThrServer(Server):
    def __init__(self, server_id, policy, app, server_config):
        super().__init__(server_id, policy, app, server_config)

    def update_policy(self):
        return

    # Get sprinting probability from Thr_Policy, and choose sprint or not by this probability, and get immediate reward
    def take_action(self):
        dist, _ = self.policy(torch.tensor([self.app.get_delta_utility()]))
        self.action = dist.sample()
        self.taken_action, self.reward = self.get_action_delta_utility()
