import os
from torch import nn
from torch.distributions import Categorical
import torch
import numpy as np

import applications
import policies

class Server:
    def __init__(self, server_id, policy, app, path, server_config):
        self.server_id = server_id
        self.rack_state = 0         # initial state: Normal
        self.server_state = 0       # initial state: Active
        self.policy = policy        # learning policy
        self.app = app
        self.path = path            # location of storing files
        self.recovery_cost = server_config["recovery_cost"]
        self.cooling_prob = server_config["cooling_prob"]   # prob. of staying cooling state
        self.reward = 0
        self.reward_history = []
        self.action = 1
        self.discount_factor = server_config["discount_factor"]
        self.frac_sprinters = torch.zeros(1)

    def get_action_reward(self, action):
        if self.rack_state == 1:
            return 1, -self.recovery_cost + self.app.get_recovery_utility()
        elif self.server_state == 1:
            return 1, self.app.get_cooling_utility()
        else:
            if action == 0:
                return 0, self.app.get_sprinting_utility()
            else:
                return 1, self.app.get_cooling_utility()

    # update application state, rack_state, server_state, and fractional number of sprinters.
    def update_state(self, rack_state, frac_sprinters):
        self.app.update_state(self.action)
        self.rack_state = rack_state
        self.frac_sprinters = torch.tensor(frac_sprinters)

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

    def run_server(self, rack_state, frac_sprinters, iteration):
        self.update_state(rack_state, frac_sprinters)
        self.update_policy()
        self.take_action()
        return self.action, self.reward

    # write reward into files
    def print_reward(self):
        file_path = os.path.join(self.path, f"server_{self.server_id}_reward.txt")
        with open(file_path, 'w+') as file:
            for r in self.reward_history:
                file.write(f"{str(r)}\n")



# Server with Actor-Critic policy
class ACServer(Server):
    def __init__(self, server_id, policy, app, path, server_config, optimizer):
        super().__init__(server_id, policy, app, path, server_config)
        self.actor_optimizer = optimizer[0]
        self.critic_optimizer = optimizer[1]
        self.state_value = torch.tensor([0.0])
        self.threshold = torch.tensor([0.0])
        self.action_prob_dist = Categorical(torch.tensor([0.0, 1.0]))
        self.a_next_state_tensor = None
        self.c_next_state_tensor = None
        self.update_actor = True

    def update_state(self, rack_state, frac_sprinters):
        super().update_state(rack_state, frac_sprinters)

        rack_state_tensor = torch.tensor([self.rack_state])
        server_state_tensor = torch.tensor([self.server_state])
        app_state_tensor = torch.tensor([self.app.get_current_state()])

        self.a_next_state_tensor = torch.cat((app_state_tensor, self.frac_sprinters))
        self.c_next_state_tensor = torch.cat((rack_state_tensor, server_state_tensor,
                                              app_state_tensor, self.frac_sprinters))

    # Update Actor and Critic networks' parameters
    def update_policy(self):
        next_state_value = self.policy.forward_critic(self.c_next_state_tensor)
        advantage = self.reward + self.discount_factor * next_state_value - self.state_value

        actor_loss = -self.action_prob_dist.log_prob(self.action) * advantage.detach()

        loss_fn = nn.MSELoss()
        critic_loss = loss_fn(self.state_value, self.reward + self.discount_factor * next_state_value)

        # Update the actor
        if self.update_actor:
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

        # Update the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

    # get threshold value and state value from AC_Policy network, choose sprint or not and get immediate reward
    def take_action(self):
        input_tensor = [self.a_next_state_tensor, self.c_next_state_tensor]
        action_probs, self.state_value = self.policy(input_tensor)
        self.action_prob_dist = Categorical(action_probs)
        action = self.action_prob_dist.sample().item()
        self.action, self.reward = self.get_action_reward(action)
        if self.rack_state == 0 and self.server_state == 0:
            self.update_actor = True
        else:
            self.update_actor = False


#  Server with threshold policy.
#  It is a fixed policy, so it doesn't need update policy
class ThrServer(Server):
    def __init__(self, server_id, policy, app, path, server_config):
        super().__init__(server_id, policy, app, path, server_config)

    def update_policy(self):
        return

    # Get sprinting probability from Thr_Policy, and choose sprint or not by this probability, and get immediate reward
    def take_action(self):
        action_probs, _ = self.policy(torch.tensor([self.app.get_sprinting_utility() - self.app.get_cooling_utility()]))
        action = Categorical(action_probs).sample().item()
        self.action, self.reward = self.get_action_reward(action)