"""Agent that learns how to play a SNES game by using RLLib algorithms"""

import retro
import argparse
import ray
from ray.rllib.agents import ppo, impala, dqn, registry
from ray.tune import register_env
from ray.tune.logger import pretty_print
import envs
import models
import time
import numpy as np
from functools import partial
import rnd
import json
import subprocess
import gym
import copy


def wrap_env(env, rewardscaling=1, skipframes=4, maxpoolframes=1, pad_action=None, keepcolor=False,
             stackframes=4, timepenalty=0, makemovie=None, makeprocessedmovie=None, cliprewards=False):
    """Wraps a game environment with many preprocessing options

        base: base environment to wrap
        skipframes: skip n-1 out of n frames
        maxpoolframes: compute maxpooling of n consecutive non-skipped frames. Useful to prevent Atari video issues
        pad_action: action to use for skipped frames. If None, repeat las action
        keepcolor: keep colored frames. Else, cast all frames to grayscale
        stackframes: stack n frames before feeding them as inputs for the RL agen
        timepenalty: penalty to add to every game frame
        makemovie: save videos of episodes. Valid modes: "all" to record all episodes, "best" to record best episodes
        makeprocessedmovie: save videos of episodes in the format the RL agent sees them. Similar parameters to
            makemovie
        cliprewards: clip rewards to range [-1, 1]
    """
    env = envs.NoopResetEnv(env)
    env = envs.RewardScaler(env, rewardscaling)
    if cliprewards:
        env = envs.RewardClipper(env)
    env = envs.SkipFrames(env, skip=skipframes, pad_action=pad_action, maxpool=maxpoolframes)
    if makemovie is not None:
        env = envs.MovieRecorder(env, fileprefix="raw", mode=makemovie)
    env = envs.WarpFrame(env, togray=not keepcolor)
    if makeprocessedmovie is not None:
        env = envs.ProcessedMovieRecorder(env, fileprefix="processed", mode=makeprocessedmovie)
    env = envs.FrameStack(env, stackframes)
    env = envs.RewardTimeDump(env, timepenalty)
    return env


def retro_env_creator(game, state, **kwargs):
    """Returns a function that creates a new retro environment the given game, state, and wrapper configuration"""
    base = retro.make(game=game, state=state)
    base = envs.ButtonsRemapper(base, game)
    return wrap_env(base, **kwargs)


def register_retro(game, state, registername="retro-v0", **kwargs):
    """Registers a new retro environment with the given game, state, and wrapper configuration

    Returns a creator function that can be used to instantiate the registered environment on demand.
    """
    env_creator = lambda env_config: retro_env_creator(game, state, **kwargs)
    register_env(registername, env_creator)
    return partial(env_creator, {})


def gym_atari_env_creator(game, **kwargs):
    """Returns a function that creates a new gym atari environment with given game, state, and wrapper configuration"""
    base = gym.make(game)
    return wrap_env(base, **kwargs)


def register_gym_atari(game, registername="retro-v0", **kwargs):
    """Registers a new gym atari environment the given game, state, and wrapper configuration

    Returns a creator function that can be used to instantiate the registered environment on demand.
    """
    wrapconf = copy.copy(kwargs)
    if "state" in wrapconf:
        del wrapconf["state"]
    env_creator = lambda env_config: gym_atari_env_creator(game, **wrapconf)
    register_env(registername, env_creator)
    return partial(env_creator, {})


BACKENDS = {
    "retro": {
        "register_func": register_retro
    },
    "gym-atari": {
        "register_func": register_gym_atari
    }
}


def register_retro(game, state, **kwargs):
    """Registers a given retro game as a ray environment

    The environment is registered with name 'retro-v0'
    """
    base = retro.make(game=game, state=state)
    base = envs.ButtonsRemapper(base, game)
    env_creator = lambda env_config: make_env(base=base, **kwargs)
    register_env("retro-v0", env_creator)
    return partial(env_creator, {})


def register_atari(game, **kwargs):
    """Registers a given gym Atari game as a ray environment

    The environment is registered with name 'retro-v0'
    """
    base = gym.make(game)
    env_creator = lambda env_config: make_env(base=base, **kwargs)
    register_env("retro-v0", env_creator)
    return partial(env_creator, {})


"""Algorithm configuration parameters."""
ALGORITHMS = {
    # Parameters from https://github.com/ray-project/ray/blob/master/python/ray/rllib/tuned_examples/pong-rainbow.yaml
    "DQN": {  # DQN Rainbow
        "default_conf": dqn.DEFAULT_CONFIG,
        "conf": {
            "num_atoms": 51,
            "noisy": True,
            "lr": 1e-4,
            "learning_starts": 10000,
            "exploration_fraction": 0.1,
            "exploration_final_eps": 0,
            "schedule_max_timesteps": 2000000,
            "prioritized_replay_alpha": 0.5,
            "beta_annealing_fraction": 0.2,
            "final_prioritized_replay_beta": 1.0,
            "n_step": 3,
            "gpu": True
        }
    },
    # Parameters from https://github.com/ray-project/ray/blob/master/python/ray/rllib/tuned_examples/atari-ppo.yaml
    "PPO": {
        "default_conf": ppo.DEFAULT_CONFIG,
        "conf": {
            "lambda": 0.95,
            "kl_coeff": 0.5,
            "clip_param": 0.1,
            "entropy_coeff": 0.01,
            "sample_batch_size": 500,
            "num_sgd_iter": 10,
            "num_envs_per_worker": 1,
            "batch_mode": "truncate_episodes",
            "observation_filter": "NoFilter",
            "vf_share_layers": True,
            "num_gpus": 1,
            "lr_schedule": [
                [0, 0.0005],
                [20000000, 0.000000000001],
            ]
        }
    },
    # Parameters from https://github.com/ray-project/ray/blob/master/python/ray/rllib/tuned_examples/atari-ppo.yaml
    # TODO: testing
    "PPORND": {
        "default_conf": rnd.DEFAULT_CONFIG,
        "conf": {
            "lambda": 0.95,
            "kl_coeff": 0.5,
            "clip_param": 0.1,
            "entropy_coeff": 0.01,
            "sample_batch_size": 500,
            "num_sgd_iter": 10,
            "num_envs_per_worker": 1,
            "batch_mode": "truncate_episodes",
            "observation_filter": "NoFilter",
            "vf_share_layers": True,
            "num_gpus": 1,
            "lr_schedule": [
                [0, 0.0005],
                [20000000, 0.000000000001],
            ]
        }
    },
    # Parameters from https://github.com/ray-project/ray/blob/master/python/ray/rllib/tuned_examples/atari-impala.yaml
    #  and IMPALA paper https://arxiv.org/abs/1802.01561 Appendix G
    "IMPALA": {
        "default_conf": impala.DEFAULT_CONFIG,
        "conf": {
            'sample_batch_size': 20,  # Unroll length
            'train_batch_size': 32,
            'num_envs_per_worker': 1,
            'lr_schedule': [
                [0, 0.0006],
                [200000000, 0.000000000001],
            ],
            "grad_clip": 40.0,
            "opt_type": "rmsprop",
            "momentum": 0.0,
            "epsilon": 0.01,
            #"num_data_loader_buffers": 4,
            #"minibatch_buffer_size": 4,
            #"num_sgd_iter": 2
            # Ideal use setting should be 1 GPU, 80 workers
        }
    },
    # Random agent for testing purposes
    "random": {
        "default_conf": {},
        "conf": {}
    }
}


def get_agent_class(alg):
    """Returns the class of a known agent given its name."""
    if alg == "PPORND":
        # TODO: testing
        return rnd.PPORNDAgent
    else:
        return registry.get_agent_class(alg)


def create_config(alg="PPO", workers=4, entropycoeff=None, lstm=None, model=None):
    """Returns a learning algorithm configuration"""
    if alg not in ALGORITHMS:
        raise ValueError(f"Unknown algorithm {alg}, must be one of {list(ALGORITHMS.keys())}")
    config = {**ALGORITHMS[alg]["default_conf"], **ALGORITHMS[alg]["conf"], **{"num_workers": workers}}
    if entropycoeff is not None:
        config["entropy_coeff"] = np.sign(config["entropy_coeff"]) * entropycoeff  # Each alg uses different sign
    if model is not None:
        config['model'] = {
            "custom_model": model
        }
    if lstm is not None:
        config['model'] = {
            **config['model'],
            "use_lstm": True,
            "max_seq_len": lstm,
            "lstm_cell_size": 256,
            "lstm_use_prev_action_reward": True
        }
    return config


def get_node_ips():
    """Returns a set with all IP addressess of nodes in the Ray cluster"""
    @ray.remote
    def f():
        time.sleep(0.01)
        return ray.services.get_node_ip_address()

    return set(ray.get([f.remote() for _ in range(1000)]))


def train(config, alg, checkpoint=None):
    """Trains a policy network"""
    agent = get_agent_class(alg)(config=config, env="retro-v0")
    if checkpoint is not None:
        try:
            agent.restore(checkpoint)
            print(f"Resumed checkpoint {checkpoint}")
        except:
            print("Checkpoint not found: restarted policy network from scratch")
    else:
        print("Started policy network from scratch")

    for i in range(1000000):
        # Perform one iteration of training the policy with the algorithm
        result = agent.train()
        print(pretty_print(result))

        if i % 50 == 0:
            checkpoint = agent.save()
            print("checkpoint saved at", checkpoint)


def test(config, alg, checkpoint=None, testdelay=0, render=False, envcreator=None, maxepisodelen=10000000):
    """Tests and renders a previously trained model"""
    if alg == "random":
        env = envcreator()
    else:
        agent = get_agent_class(alg)(config=config, env="retro-v0")
        if checkpoint is None:
            raise ValueError(f"A previously trained checkpoint must be provided for algorithm {alg}")
        agent.restore(checkpoint)
        env = agent.local_evaluator.env

    while True:
        state = env.reset()
        done = False
        reward_total = 0.0
        step = 0
        while not done and step < maxepisodelen:
            if alg == "random":
                action = np.random.choice(range(env.action_space.n))
            else:
                action = agent.compute_action(state)
            next_state, reward, done, _ = env.step(action)
            time.sleep(testdelay)
            reward_total += reward
            if render:
                env.render()
            state = next_state
            step = step + 1
        print("Episode reward", reward_total)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Agent that learns how to play a retro game by using RLLib.')
    parser.add_argument('game', type=str, help='Game to play. Must be a valid Gym Retro game')
    parser.add_argument('--state', type=str, default=None,
                        help='State (level) of the game to play. Only used for retro backend')
    parser.add_argument('--backend', type=str, default="retro",
                        help=f'Emulator backend to use, from {list(BACKENDS.keys())}. Default: "retro"')
    parser.add_argument('--checkpoint', type=str, help='Checkpoint file from which to load learning progress')
    parser.add_argument('--test', action='store_true', help='Run in test mode (no policy updates, render environment)')
    parser.add_argument('--skipframes', type=int, default=4, help='Run the environment skipping N-1 out of N frames')
    parser.add_argument('--maxpoolframes', type=int, default=1, help='Maxpool the last N frames in the skipped frames')
    parser.add_argument('--padaction', type=int, default=None, help='Index of action used to pad skipped frames')
    parser.add_argument('--keepcolor', action='store_true', help='Keep colors in image processing')
    parser.add_argument('--stackframes', type=int, default=4, help='Give the model a stack of the latest N frames')
    parser.add_argument('--testdelay', type=float, default=0,
                        help='Introduced delay between test frames. Useful for debugging')
    parser.add_argument('--render', action='store_true', help='Render test episodes')
    parser.add_argument('--makemovie', type=str, default=None,
                        help='Save videos of test episodes. '
                             'Valid modes: "all" to record all episodes, '
                             '"best" to record best episodes')
    parser.add_argument('--makeprocessedmovie', type=str, default=None,
                        help='Save videos of test episodes in form of processed frames. '
                             'Modes similar to those of --makemovie')
    parser.add_argument('--maxepisodelen', type=int, default=1000000, help='Maximum length of test episodes')
    parser.add_argument('--algorithm', type=str, default="IMPALA",
                        help=f'Algorithm to use for training: {list(ALGORITHMS.keys())}')
    parser.add_argument('--model', type=str, default=None,
                        help=f'Deep network model to use for training: {[None] + list(models.MODELS.keys())}')
    parser.add_argument('--lstm', type=int, default=None,
                        help=f'Length of sequences to feed into the LSTM layer (default: no LSTM layer)')
    parser.add_argument('--workers', type=int, default=4, help='Number of workers to use during training')
    parser.add_argument('--localworkers', type=int, default=None, help='Number of local workers to use in this machine (default: equal to "workers")')
    parser.add_argument('--timepenalty', type=float, default=0, help='Reward penalty to apply to each timestep')
    parser.add_argument('--entropycoeff', type=float, default=None, help='Entropy bonus to apply to diverse actions')
    parser.add_argument('--cliprewards', action="store_true", help='Clip rewards to {-1, 0, +1}')
    parser.add_argument('--waitforinput', action="store_true",
                        help="Start ray, but don't start training until user input is received. Useful to connect "
                             "other ray nodes to this manager before training starts.")
    parser.add_argument('--waitfornodes', type=int, default=None,
                        help="Wait until at least this number of nodes is available in the Ray cluster")
    parser.add_argument('--redisaddress', type=str, default=None, help="Redis address of Ray server to connect to")
    parser.add_argument('--importroms', type=str, default=None, help='Import roms from given folder before start')

    args = parser.parse_args()

    if args.localworkers is None:
        args.localworkers = args.workers

    # Shutdown other ray processes to avoid runnig several trainings in parallel
    ray.shutdown()

    # Import ROMs if requested
    if args.importroms is not None:
        subprocess.run(["python", "-m", "retro.import", args.importroms])

    # Check backend
    if args.backend not in BACKENDS:
        raise ValueError(f"Unknown backend {args.backend}, must be one of {list(BACKENDS.keys())}")
    backend = BACKENDS[args.backend]

    # Register environment
    envcreator = backend["register_func"](game=args.game, state=args.state, skipframes=args.skipframes,
                                          maxpoolframes=args.maxpoolframes, pad_action=args.padaction,
                                          keepcolor=args.keepcolor, stackframes=args.stackframes,
                                          timepenalty=args.timepenalty, makemovie=args.makemovie,
                                          makeprocessedmovie=args.makeprocessedmovie, cliprewards=args.cliprewards)

    config = create_config(args.algorithm, workers=args.workers, entropycoeff=args.entropycoeff, model=args.model,
                           lstm=args.lstm)
    print(f"Config: {json.dumps(config, indent=4, sort_keys=True)}")

    ray.init(num_cpus=args.localworkers, num_gpus=1, redis_address=args.redisaddress)

    if args.waitforinput:
        input("Press key to start")

    # Get a list of the IP addresses of the nodes that have joined the cluster.
    nodes = get_node_ips()
    print(f"Ray nodes in the cluster: {nodes}")
    if args.waitfornodes:
        print(f"Available nodes ({len(nodes)}) less than required nodes ({args.waitfornodes}). Waiting...")
        while len(nodes) < args.waitfornodes:
            time.sleep(5)
            nodes = get_node_ips()

    if args.test:
        test(config, args.algorithm, checkpoint=args.checkpoint, testdelay=args.testdelay,
             render=args.render, envcreator=envcreator, maxepisodelen=args.maxepisodelen)
    else:
        train(config, args.algorithm, checkpoint=args.checkpoint)
