import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.distributions.categorical import Categorical
from torch.distributions.normal import Normal
from torch.utils.tensorboard import SummaryWriter

from cleanrl.common import preprocess_obs_space, preprocess_ac_space
import argparse
from distutils.util import strtobool
import numpy as np
import gym
from gym.wrappers import TimeLimit, Monitor
from gym.spaces import Discrete, Box, MultiBinary, MultiDiscrete, Space
import time
import random
import os

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='A2C agent')
    # Common arguments
    parser.add_argument('--exp-name', type=str, default=os.path.basename(__file__).strip(".py"),
                        help='the name of this experiment')
    parser.add_argument('--gym-id', type=str, default="CartPole-v0",
                        help='the id of the gym environment')
    parser.add_argument('--learning-rate', type=float, default=7e-4,
                        help='the learning rate of the optimizer')
    parser.add_argument('--seed', type=int, default=1,
                        help='seed of the experiment')
    parser.add_argument('--episode-length', type=int, default=0,
                        help='the maximum length of each episode')
    parser.add_argument('--total-timesteps', type=int, default=4000000,
                        help='total timesteps of the experiments')
    parser.add_argument('--torch-deterministic', type=lambda x:bool(strtobool(x)), default=True, nargs='?', const=True,
                        help='if toggled, `torch.backends.cudnn.deterministic=False`')
    parser.add_argument('--cuda', type=lambda x:bool(strtobool(x)), default=True, nargs='?', const=True,
                        help='if toggled, cuda will not be enabled by default')
    parser.add_argument('--prod-mode', type=lambda x:bool(strtobool(x)), default=False, nargs='?', const=True,
                        help='run the script in production mode and use wandb to log outputs')
    parser.add_argument('--capture-video', type=lambda x:bool(strtobool(x)), default=False, nargs='?', const=True,
                        help='weather to capture videos of the agent performances (check out `videos` folder)')
    parser.add_argument('--wandb-project-name', type=str, default="cleanRL",
                        help="the wandb's project name")
    parser.add_argument('--wandb-entity', type=str, default=None,
                        help="the entity (team) of wandb's project")
    
    # Algorithm specific arguments
    parser.add_argument('--gamma', type=float, default=0.99,
                        help='the discount factor gamma')
    parser.add_argument('--max-grad-norm', type=float, default=0.5,
                        help='the maximum norm for the gradient clipping')
    args = parser.parse_args()
    if not args.seed:
        args.seed = int(time.time())

# TRY NOT TO MODIFY: setup the environment
experiment_name = f"{args.gym_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
writer = SummaryWriter(f"runs/{experiment_name}")
writer.add_text('hyperparameters', "|param|value|\n|-|-|\n%s" % (
        '\n'.join([f"|{key}|{value}|" for key, value in vars(args).items()])))
if args.prod_mode:
    import wandb
    wandb.init(project=args.wandb_project_name, entity=args.wandb_entity, sync_tensorboard=True, config=vars(args), name=experiment_name, monitor_gym=True, save_code=True)
    writer = SummaryWriter(f"/tmp/{experiment_name}")


# TRY NOT TO MODIFY: seeding
device = torch.device('cuda' if torch.cuda.is_available() and args.cuda else 'cpu')
env = gym.make(args.gym_id)
random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.deterministic = args.torch_deterministic
env.seed(args.seed)
env.action_space.seed(args.seed)
env.observation_space.seed(args.seed)
input_shape, preprocess_obs_fn = preprocess_obs_space(env.observation_space, device)
output_shape = preprocess_ac_space(env.action_space)
# respect the default timelimit
assert isinstance(env, TimeLimit) or int(args.episode_length), "the gym env does not have a built in TimeLimit, please specify by using --episode-length"
if isinstance(env, TimeLimit):
    if int(args.episode_length):
        env._max_episode_steps = int(args.episode_length)
    args.episode_length = env._max_episode_steps
else:
    env = TimeLimit(env, int(args.episode_length))
if args.capture_video:
    env = Monitor(env, f'videos/{experiment_name}')

# ALGO LOGIC: initialize agent here:
class Policy(nn.Module):
    def __init__(self):
        super(Policy, self).__init__()
        self.fc1 = nn.Linear(input_shape, 120)
        self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(84, output_shape)

    def forward(self, x):
        x = preprocess_obs_fn(x)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x

pg = Policy().to(device)
optimizer = optim.Adam(list(pg.parameters()), lr=args.learning_rate)
loss_fn = nn.MSELoss()

# TRY NOT TO MODIFY: start the game
global_step = 0
while global_step < args.total_timesteps:
    next_obs = np.array(env.reset())
    actions = np.empty((args.episode_length,), dtype=object)
    rewards, dones = np.zeros((2, args.episode_length))
    obs = np.empty((args.episode_length,) + env.observation_space.shape)
    
    # ALGO LOGIC: put other storage logic here
    neglogprobs = torch.zeros((args.episode_length,), device=device)
    # TRY NOT TO MODIFY: prepare the execution of the game.
    for step in range(args.episode_length):
        global_step += 1
        obs[step] = next_obs.copy()
        
        # ALGO LOGIC: put action logic here
        logits = pg.forward(obs[step:step+1])

        # ALGO LOGIC: `env.action_space` specific logic
        if isinstance(env.action_space, Discrete):
            probs = Categorical(logits=logits)
            action = probs.sample()
            actions[step], neglogprobs[step] = action.tolist()[0], -probs.log_prob(action)
    
        elif isinstance(env.action_space, MultiDiscrete):
            logits_categories = torch.split(logits, env.action_space.nvec.tolist(), dim=1)
            action = []
            probs_categories = []
            neglogprob = torch.zeros((logits.shape[0]), device=device)
            for i in range(len(logits_categories)):
                probs_categories.append(Categorical(logits=logits_categories[i]))
                if len(action) != env.action_space.shape:
                    action.append(probs_categories[i].sample())
                neglogprob -= probs_categories[i].log_prob(action[i])
            action = torch.stack(action).transpose(0, 1).tolist()
            actions[step], neglogprobs[step] = action[0], neglogprob
        
        # TRY NOT TO MODIFY: execute the game and log data.
        next_obs, rewards[step], dones[step], _ = env.step(actions[step])
        next_obs = np.array(next_obs)
        if dones[step]:
            break
    
    # ALGO LOGIC: training.
    # calculate the discounted rewards, or namely, returns
    returns = np.zeros_like(rewards)
    for t in reversed(range(rewards.shape[0]-1)):
        returns[t] = rewards[t] + args.gamma * returns[t+1] * (1-dones[t])
    # advantages are returns - baseline, value estimates in our case
    advantages = returns
    
    pg_loss = torch.Tensor(advantages).to(device) * neglogprobs
    loss = (pg_loss).mean()
    
    optimizer.zero_grad()
    loss.backward()
    nn.utils.clip_grad_norm_(list(pg.parameters()), args.max_grad_norm)
    optimizer.step()

    # TRY NOT TO MODIFY: record rewards for plotting purposes
    writer.add_scalar("charts/episode_reward", rewards.sum(), global_step)
    writer.add_scalar("losses/policy_loss", pg_loss.mean().item(), global_step)
env.close()
writer.close()
