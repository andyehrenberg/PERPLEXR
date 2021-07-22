import copy

import torch
import torch.nn as nn
import torch.nn.functional as F
from level_replay.algo.dqn import DQN, RainbowDQN, SimpleDQN
from torch.nn.utils import clip_grad_norm_


class Rainbow(object):
    def __init__(self, args):
        self.device = args.device
        self.action_space = args.num_actions
        self.atoms = args.atoms
        self.V_min = args.V_min
        self.V_max = args.V_max
        self.support = torch.linspace(args.V_min, args.V_max, self.atoms).to(
            device=args.device
        )  # Support (range) of z
        self.delta_z = (args.V_max - args.V_min) / (self.atoms - 1)
        self.batch_size = args.batch_size
        self.n = args.multi_step
        self.norm_clip = args.norm_clip
        self.PER = args.PER
        self.gamma = args.gamma

        self.Q = RainbowDQN(args, self.action_space).to(self.device)
        self.Q_target = copy.deepcopy(self.Q)
        self.Q_optimizer = getattr(torch.optim, args.optimizer)(
            self.Q.parameters(), **args.optimizer_parameters
        )

        for param in self.Q_target.parameters():
            param.requires_grad = False

        self.alpha = args.alpha
        self.min_priority = args.min_priority

        self.maybe_update_target = (
            self.polyak_target_update if args.polyak_target_update else self.copy_target_update
        )
        self.target_update_frequency = args.target_update
        self.tau = args.tau

        self.state_shape = (-1,) + args.state_dim
        self.eval_eps = args.eval_eps
        self.num_actions = args.num_actions

        # Number of training iterations
        self.iterations = 0

    def select_action(self, state, eval=False):
        with torch.no_grad():
            q = self.Q(state)
            q_s = (q * self.support).sum(2)
            action = q_s.argmax(1).reshape(-1, 1)
            return action, q_s.max(1)[0]

    def get_value(self, state):
        with torch.no_grad():
            q = self.Q(state)
            q_s = (q * self.support).sum(2)
            return q_s.max(1)[0]

    def train(self, replay_buffer):
        self.reset_noise()
        state, action, next_state, reward, not_done, seeds, ind, weights = replay_buffer.sample()

        log_p1s = self.Q(state, log=True)
        log_p1s_a = log_p1s.gather(1, action.unsqueeze(1).expand(self.batch_size, 1, self.atoms)).squeeze(1)

        with torch.no_grad():
            next_Q = self.Q(next_state)
            next_action = (next_Q * self.support).sum(2).argmax(1).reshape(-1, 1)
            self.Q_target.reset_noise()
            pns = self.Q_target(next_state)
            pns_a = pns.gather(1, next_action.unsqueeze(1).expand(self.batch_size, 1, self.atoms)).squeeze(1)
            target_Q = reward.expand(-1, self.atoms) + not_done * (self.gamma ** self.n) * pns_a

        target_Q = target_Q.clamp(min=self.V_min, max=self.V_max)  # Clamp between supported values
        # Compute L2 projection of Tz onto fixed support z
        b = (target_Q - self.V_min) / self.delta_z
        low, up = b.floor().to(torch.int64), b.ceil().to(torch.int64)
        low[(up > 0) * (low == up)] -= 1
        up[(low < (self.atoms - 1)) * (low == up)] += 1

        # Distribute probability of Tz
        m = state.new_zeros(self.batch_size, self.atoms)
        offset = (
            torch.linspace(0, ((self.batch_size - 1) * self.atoms), self.batch_size)
            .unsqueeze(1)
            .expand(self.batch_size, self.atoms)
            .to(action)
        )
        m.view(-1).index_add_(
            0, (low + offset).view(-1), (pns_a * (up.float() - b)).view(-1)
        )  # m_l = m_l + p(s_t+n, a*)(u - b)
        m.view(-1).index_add_(
            0, (up + offset).view(-1), (pns_a * (b - low.float())).view(-1)
        )  # m_u = m_u + p(s_t+n, a*)(b - l)

        loss = -torch.sum(m * log_p1s_a, 1)  # Cross-entropy loss (minimises DKL(m||p(s_t, a_t)))
        self.Q.zero_grad()
        mean_loss = (weights * loss).mean()
        mean_loss.backward()  # Backpropagate importance-weighted minibatch loss
        clip_grad_norm_(self.Q.parameters(), self.norm_clip)  # Clip gradients by L2 norm
        grad_magnitude = list(self.Q.named_parameters())[-2][1].grad.clone().norm()
        self.Q_optimizer.step()

        # Update target network by polyak or full copy every X iterations.
        self.iterations += 1
        self.maybe_update_target()

        if self.PER:
            priority = loss.clamp(min=self.min_priority).cpu().data.numpy().flatten()
            replay_buffer.update_priority(ind, priority)

        return mean_loss, grad_magnitude

    def huber(self, x):
        return torch.where(x < self.min_priority, 0.5 * x.pow(2), self.min_priority * x).mean()

    def polyak_target_update(self):
        for param, target_param in zip(self.Q.parameters(), self.Q_target.parameters()):
            target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    def copy_target_update(self):
        if self.iterations % self.target_update_frequency == 0:
            self.Q_target.load_state_dict(self.Q.state_dict())

    def reset_noise(self):
        self.Q.reset_noise()

    def save(self, filename):
        torch.save(self.iterations, filename + "iterations")
        torch.save(self.Q.state_dict(), f"{filename}Q_{self.iterations}")
        torch.save(self.Q_optimizer.state_dict(), filename + "optimizer")

    def load(self, filename):
        self.iterations = torch.load(filename + "iterations")
        self.Q.load_state_dict(torch.load(f"{filename}Q_{self.iterations}"))
        self.Q_target = copy.deepcopy(self.Q)
        self.Q_optimizer.load_state_dict(torch.load(filename + "optimizer"))


class DDQN(object):
    def __init__(self, args):
        self.device = args.device
        self.action_space = args.num_actions
        self.batch_size = args.batch_size
        self.norm_clip = args.norm_clip
        self.gamma = args.gamma

        if args.simple_dqn:
            self.Q = SimpleDQN(args, self.action_space).to(self.device)
        else:
            self.Q = DQN(args, self.action_space).to(self.device)

        self.Q_target = copy.deepcopy(self.Q)
        self.Q_optimizer = getattr(torch.optim, args.optimizer)(
            self.Q.parameters(), **args.optimizer_parameters
        )
        for param in self.Q_target.parameters():
            param.requires_grad = False

        self.PER = args.PER
        self.n_step = args.multi_step

        self.alpha = args.alpha
        self.min_priority = args.min_priority

        # Target update rule
        self.maybe_update_target = (
            self.polyak_target_update if args.polyak_target_update else self.copy_target_update
        )
        self.target_update_frequency = args.target_update
        self.tau = args.tau

        # Evaluation hyper-parameters
        self.state_shape = (-1,) + args.state_dim
        self.eval_eps = args.eval_eps
        self.num_actions = args.num_actions

        # For seed bar chart
        self.seed_weights = {i: 0 for i in range(args.start_level, args.start_level + args.num_train_seeds)}

        # Number of training iterations
        self.iterations = 0

    def select_action(self, state, eval=False):
        with torch.no_grad():
            q = self.Q(state)
            action = q.argmax(1).reshape(-1, 1)
            return action, q.max(1)[0]

    def get_value(self, state):
        with torch.no_grad():
            q = self.Q(state)
            value = q.max(1)[0]
            return value

    def train(self, replay_buffer, ere=False, c_k=None):
        if ere:
            (
                state,
                action,
                next_state,
                reward,
                not_done,
                seeds,
                ind,
                weights,
            ) = replay_buffer.sample_priority_only_batch(c_k)
        else:
            state, action, next_state, reward, not_done, seeds, ind, weights = replay_buffer.sample()

        for idx, seed in enumerate(seeds):
            s = seed.cpu().numpy()[0]
            if self.PER and not ere:
                self.seed_weights[s] = self.seed_weights.get(s, 0) + weights[idx].cpu().numpy()[0]
            else:
                self.seed_weights[s] = self.seed_weights.get(s, 0) + 1

        with torch.no_grad():
            next_action = self.Q(next_state).argmax(1).reshape(-1, 1)
            target_Q = reward + not_done * (self.gamma ** self.n_step) * self.Q_target(next_state).gather(
                1, next_action
            )

        current_Q = self.Q(state).gather(1, action)

        loss = (weights * F.smooth_l1_loss(current_Q, target_Q, reduction="none")).mean()

        self.Q_optimizer.zero_grad()
        loss.backward()  # Backpropagate importance-weighted minibatch loss
        clip_grad_norm_(self.Q.parameters(), self.norm_clip)  # Clip gradients by L2 norm
        grad_magnitude = list(self.Q.named_parameters())[-2][1].grad.clone().norm()
        self.Q_optimizer.step()

        # Update target network by polyak or full copy every X iterations.
        self.iterations += 1
        self.maybe_update_target()

        if self.PER and not ere:
            priority = ((current_Q - target_Q).abs() + 1e-10).cpu().data.numpy().flatten()
            replay_buffer.update_priority(ind, priority)

        return loss, grad_magnitude

    def train_with_online_target(self, replay_buffer, online):
        state, action, next_state, reward, not_done, seeds, ind, weights = replay_buffer.sample()

        for idx, seed in enumerate(seeds):
            s = seed.cpu().numpy()[0]
            if self.PER:
                self.seed_weights[s] = self.seed_weights.get(s, 0) + weights[idx].cpu().numpy()[0]
            else:
                self.seed_weights[s] = self.seed_weights.get(s, 0) + 1

        with torch.no_grad():
            target_Q = reward + not_done * (self.gamma ** self.n_step) * online.get_value(next_state, 0, 0)

        current_Q = self.Q(state).gather(1, action)

        loss = (weights * F.smooth_l1_loss(current_Q, target_Q, reduction="none")).mean()

        self.Q_optimizer.zero_grad()
        loss.backward()  # Backpropagate importance-weighted minibatch loss
        clip_grad_norm_(self.Q.parameters(), self.norm_clip)  # Clip gradients by L2 norm
        grad_magnitude = list(self.Q.named_parameters())[-2][1].grad.clone().norm()
        self.Q_optimizer.step()

        # Update target network by polyak or full copy every X iterations.
        self.iterations += 1
        self.maybe_update_target()

        if self.PER:
            priority = ((current_Q - target_Q).abs() + 1e-10).cpu().data.numpy().flatten()
            replay_buffer.update_priority(ind, priority)

        return loss, grad_magnitude

    def huber(self, x):
        return torch.where(x < self.min_priority, 0.5 * x.pow(2), self.min_priority * x).mean()

    def polyak_target_update(self):
        for param, target_param in zip(self.Q.parameters(), self.Q_target.parameters()):
            target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    def copy_target_update(self):
        if self.iterations % self.target_update_frequency == 0:
            self.Q_target.load_state_dict(self.Q.state_dict())

    def save(self, filename):
        torch.save(self.iterations, filename + "iterations")
        torch.save(self.Q.state_dict(), f"{filename}Q_{self.iterations}")
        torch.save(self.Q_optimizer.state_dict(), filename + "optimizer")

    def load(self, filename):
        self.iterations = torch.load(filename + "iterations")
        self.Q.load_state_dict(torch.load(f"{filename}Q_{self.iterations}"))
        self.Q_target = copy.deepcopy(self.Q)
        self.Q_optimizer.load_state_dict(torch.load(filename + "optimizer"))


class Conv_Q(nn.Module):
    def __init__(self, frames, num_actions):
        super(Conv_Q, self).__init__()
        self.c1 = nn.Conv2d(frames, 32, kernel_size=8, stride=4)
        self.c2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.c3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.l1 = nn.Linear(3136, 512)
        self.l2 = nn.Linear(512, num_actions)

    def forward(self, state):
        q = F.relu(self.c1(state))
        q = F.relu(self.c2(q))
        q = F.relu(self.c3(q))
        q = F.relu(self.l1(q.reshape(-1, 3136)))
        return self.l2(q)


class AtariAgent(object):
    # Doesn't use IMPALA features
    def __init__(self, args):
        self.device = args.device
        self.action_space = args.num_actions
        self.batch_size = args.batch_size
        self.norm_clip = args.norm_clip
        self.gamma = args.gamma

        self.Q = Conv_Q(4, self.action_space).to(self.device)
        self.Q_target = copy.deepcopy(self.Q)
        self.Q_optimizer = getattr(torch.optim, args.optimizer)(
            self.Q.parameters(), **args.optimizer_parameters
        )

        self.PER = args.PER
        self.n_step = args.multi_step

        self.alpha = args.alpha
        self.min_priority = args.min_priority

        # Target update rule
        self.maybe_update_target = (
            self.polyak_target_update if args.polyak_target_update else self.copy_target_update
        )
        self.target_update_frequency = args.target_update
        self.tau = args.tau

        # Evaluation hyper-parameters
        self.state_shape = (-1,) + args.state_dim
        self.eval_eps = args.eval_eps
        self.num_actions = args.num_actions

        # Number of training iterations
        self.iterations = 0

    def select_action(self, state, eval=False):
        with torch.no_grad():
            q = self.Q(state)
            action = q.argmax(1).reshape(-1, 1)
            return action, None

    def train(self, replay_buffer):
        state, action, next_state, reward, not_done, seeds, ind, weights = replay_buffer.sample()

        with torch.no_grad():
            next_action = self.Q(next_state).argmax(1).reshape(-1, 1)
            target_Q = reward + not_done * (self.gamma ** self.n_step) * self.Q_target(next_state).gather(
                1, next_action
            )

        current_Q = self.Q(state).gather(1, action)

        loss = (weights * F.smooth_l1_loss(current_Q, target_Q, reduction="none")).mean()

        self.Q_optimizer.zero_grad()
        loss.backward()  # Backpropagate importance-weighted minibatch loss
        grad_magnitude = list(self.Q.named_parameters())[-2][1].grad.clone().norm()
        # clip_grad_norm_(self.Q.parameters(), self.norm_clip)  # Clip gradients by L2 norm
        self.Q_optimizer.step()

        # Update target network by polyak or full copy every X iterations.
        self.iterations += 1
        self.maybe_update_target()

        if self.PER:
            priority = ((current_Q - target_Q).abs() + 1e-10).pow(0.6).cpu().data.numpy().flatten()
            replay_buffer.update_priority(ind, priority)

        return loss, grad_magnitude

    def huber(self, x):
        return torch.where(x < self.min_priority, 0.5 * x.pow(2), self.min_priority * x).mean()

    def polyak_target_update(self):
        for param, target_param in zip(self.Q.parameters(), self.Q_target.parameters()):
            target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    def copy_target_update(self):
        if self.iterations % self.target_update_frequency == 0:
            self.Q_target.load_state_dict(self.Q.state_dict())

    def save(self, filename):
        torch.save(self.iterations, filename + "iterations")
        torch.save(self.Q.state_dict(), f"{filename}Q_{self.iterations}")
        torch.save(self.Q_optimizer.state_dict(), filename + "optimizer")

    def load(self, filename):
        self.iterations = torch.load(filename + "iterations")
        self.Q.load_state_dict(torch.load(f"{filename}Q_{self.iterations}"))
        self.Q_target = copy.deepcopy(self.Q)
        self.Q_optimizer.load_state_dict(torch.load(filename + "optimizer"))
