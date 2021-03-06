# Copyright (c) 2020 Horizon Robotics. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Suite for loading OpenAI Safety Gym environments.

**NOTE**: Mujoco requires separated installation.

(gym >= 0.10, and mujoco>=1.50)

Follow the instructions at:

https://github.com/openai/mujoco-py


Several general facts about the provided benchmark environments:
1. All have distance-based dense rewards
2. All have continual goals: after reaching a goal, the goal is reset but the
   layout keeps the same until timeout.
3. Layouts are randomized before episodes begin
4. Costs are indicator binaries (0 or 1). Every positive cost will be binarized
   to 1. Thus the total cost will be 1 if any component cost is positive.
5. level 0 has no constraints; level 1 has some unsafe elements; level 2 has
   very dense unsafe elements.

See https://github.com/openai/safety-gym/blob/f31042f2f9ee61b9034dd6a416955972911544f5/safety_gym/envs/engine.py#L97
for a complete list of default configurations.
"""

try:
    import mujoco_py
    import safety_gym
except ImportError:
    mujoco_py = None
    safety_gym = None

import functools
import numpy as np
import copy
import gym

import gin
from alf.environments import suite_gym, alf_wrappers, process_environment


def is_available():
    return (mujoco_py is not None and safety_gym is not None)


class CompleteEnvInfo(gym.Wrapper):
    """Always set the complete set of information so that the env info has a
    fixed shape (no matter whether some event occurs or not), which is required
    by ALF.

    The current safety gym env only adds a key to env info when the corresponding
    event is triggered, see:
    https://github.com/openai/safety-gym/blob/f31042f2f9ee61b9034dd6a416955972911544f5/safety_gym/envs/engine.py#L1242
    """

    def __init__(self, env, env_name):
        super().__init__(env)
        # env info keys are retrieved from:
        # https://github.com/openai/safety-gym/blob/master/safety_gym/envs/engine.py
        self._env_info_keys = [
            'cost_exception',
            'goal_met',
            'cost'  # this is the summed overall cost
        ]
        if not self._is_level0_env(env_name):
            # for level 1 and 2 envs, there are constraints cost info
            self._env_info_keys += [
                'cost_vases_contact', 'cost_pillars', 'cost_buttons',
                'cost_gremlins', 'cost_vases_displace', 'cost_vases_velocity',
                'cost_hazards'
            ]
        self._default_env_info = self._generate_default_env_info()

    def _is_level0_env(self, env_name):
        return "0-v" in env_name

    def _generate_default_env_info(self):
        env_info = {}
        for key in self._env_info_keys:
            if key == "goal_met":
                env_info[key] = False
            else:
                env_info[key] = np.float32(0.)
        return env_info

    def step(self, action):
        env_info = copy.copy(self._default_env_info)
        obs, reward, done, info = self.env.step(action)
        env_info.update(info)
        return obs, reward, done, env_info


class VectorReward(gym.Wrapper):
    """This wrapper makes the env returns a reward vector of length 3. The three
    dimensions are:

    1. distance-improvement reward indicating the delta smaller distances of
       agent<->box and box<->goal for "push" tasks, or agent<->goal for
       "goal"/"button" tasks.
    2. negative binary cost where -1 means that at least one constraint has been
       violated at the current time step (constraints vary depending on env
       configurations).
    3. a success indicator where 1 means the goal is met at the current step

    All rewards are the higher the better.
    """

    REWARD_DIMENSION = 3

    def __init__(self, env):
        super().__init__(env)
        self._reward_space = gym.spaces.Box(
            low=-float('inf'),
            high=float('inf'),
            shape=[self.REWARD_DIMENSION])

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        # Get the second and third reward from ``info``
        cost_reward = -info["cost"]
        success_reward = float(info["goal_met"])
        return obs, np.array([reward, cost_reward, success_reward],
                             dtype=np.float32), done, info

    @property
    def reward_space(self):
        return self._reward_space


gin.constant('SafetyGym.REWARD_DIMENSION', VectorReward.REWARD_DIMENSION)


@gin.configurable
def load(environment_name,
         env_id=None,
         discount=1.0,
         max_episode_steps=None,
         unconstrained=False,
         gym_env_wrappers=(),
         alf_env_wrappers=()):
    """Loads the selected environment and wraps it with the specified wrappers.

    Note that by default a ``TimeLimit`` wrapper is used to limit episode lengths
    to the default benchmarks defined by the registered environments.

    Args:
        environment_name: Name for the environment to load.
        env_id: A scalar ``Tensor`` of the environment ID of the time step.
        discount: Discount to use for the environment.
        max_episode_steps: If None or 0 the ``max_episode_steps`` will be set to
            the default step limit -1 defined in the environment. Otherwise
            ``max_episode_steps`` will be set to the smaller value of the two.
        unconstrained (bool): if True, the suite will be used just as an
            unconstrained environment. The reward will always be scalar without
            including constraints.
        gym_env_wrappers: Iterable with references to wrapper classes to use
            directly on the gym environment.
        alf_env_wrappers: Iterable with references to wrapper classes to use on
            the torch environment.

    Returns:
        An AlfEnvironment instance.
    """

    # We can directly make the env here because none of the safety gym tasks
    # is registered with a ``max_episode_steps`` argument (the
    # ``gym.wrappers.time_limit.TimeLimit`` won't be applied). But each task
    # will inherently manage the time limit through ``env.num_steps``.
    env = gym.make(environment_name)

    # fill all env info with default values
    env = CompleteEnvInfo(env, environment_name)

    # make vector reward
    if not unconstrained:
        env = VectorReward(env)

    # Have to -1 on top of the original env max steps here, because the
    # underlying gym env will output ``done=True`` when reaching the time limit
    # ``env.num_steps`` (before the ``AlfGymWrapper``), which is incorrect:
    # https://github.com/openai/safety-gym/blob/f31042f2f9ee61b9034dd6a416955972911544f5/safety_gym/envs/engine.py#L1302
    if not max_episode_steps:  # None or 0
        max_episode_steps = env.num_steps - 1
    max_episode_steps = min(env.num_steps - 1, max_episode_steps)

    return suite_gym.wrap_env(
        env,
        env_id=env_id,
        discount=discount,
        max_episode_steps=max_episode_steps,
        gym_env_wrappers=gym_env_wrappers,
        alf_env_wrappers=alf_env_wrappers)
