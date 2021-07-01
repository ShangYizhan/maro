# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import time
from os import getcwd
from typing import List, Union

from maro.rl.early_stopping import AbsEarlyStopper
from maro.rl.wrappers import AbsEnvWrapper, AgentWrapper
from maro.utils import Logger


class LocalLearner:
    """Controller for single-threaded learning workflows.

    Args:
        env_wrapper (AbsEnvWrapper): Environment wrapper instance to interact with a set of agents and collect
            experiences for learning.
        agent_wrapper (AgentWrapper): Multi-policy wrapper that interacts with the ``env_wrapper`` directly.
        num_episodes (int): Number of training episodes. Each training episode may contain one or more
            collect-update cycles, depending on how the implementation of the roll-out manager.
        num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in which
            case the roll-out will be executed until the end of the environment.
        eval_schedule (Union[int, List[int]]): Evaluation schedule. If an integer is provided, the policies will
            will be evaluated every ``eval_schedule`` episodes. If a list is provided, the policies will be evaluated
            at the end of the training episodes given in the list. In any case, the policies will be evaluated
            at the end of the last training episode. Defaults to None, in which case the policies will only be
            evaluated after the last training episode.
        eval_env (AbsEnvWrapper): An ``AbsEnvWrapper`` instance for policy evaluation. If None, ``env`` will be used
            as the evaluation environment. Defaults to None.
        early_stopper (AbsEarlyStopper): Early stopper to stop the main training loop if certain conditions on the
            environment metrics are met following an evaluation episode. Default to None.
        log_env_summary (bool): If True, the ``summary`` property of the environment wrapper will be logged at the end
            of each episode. Defaults to True.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "LOCAL_ROLLOUT_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current working
            directory.
    """

    def __init__(
        self,
        env_wrapper: AbsEnvWrapper,
        agent_wrapper: AgentWrapper,
        num_episodes: int,
        num_steps: int = -1,
        eval_schedule: Union[int, List[int]] = None,
        eval_env: AbsEnvWrapper = None,
        early_stopper: AbsEarlyStopper = None,
        log_env_summary: bool = True,
        log_dir: str = getcwd(),
    ):
        if num_steps == 0 or num_steps < -1:
            raise ValueError("num_steps must be a positive integer or -1")

        self.logger = Logger("LOCAL_LEARNER", dump_folder=log_dir)
        self.env = env_wrapper
        self.eval_env = eval_env if eval_env else self.env
        self.agent = agent_wrapper

        self.num_episodes = num_episodes
        self._num_steps = num_steps if num_steps > 0 else float("inf")

        # evaluation schedule
        if eval_schedule is None:
            self._eval_schedule = []
        elif isinstance(eval_schedule, int):
            num_eval_schedule = num_episodes // eval_schedule
            self._eval_schedule = [eval_schedule * i for i in range(1, num_eval_schedule + 1)]
        else:
            self._eval_schedule = eval_schedule
            self._eval_schedule.sort()

        # always evaluate after the last episode
        if not self._eval_schedule or num_episodes != self._eval_schedule[-1]:
            self._eval_schedule.append(num_episodes)

        self.logger.info(f"Policy will be evaluated at the end of episodes {self._eval_schedule}")
        self._eval_point_index = 0

        self.early_stopper = early_stopper

        self._log_env_summary = log_env_summary

    def run(self):
        """Entry point for executing a learning workflow."""
        for ep in range(1, self.num_episodes + 1):
            self._train(ep)
            if ep == self._eval_schedule[self._eval_point_index]:
                self._eval_point_index += 1
                self._evaluate()
                # early stopping check
                if self.early_stopper:
                    self.early_stopper.push(self.eval_env.summary)
                    if self.early_stopper.stop():
                        return

    def _train(self, ep: int):
        """Collect simulation data for training."""
        t0 = time.time()
        num_experiences_collected = 0

        self.agent.explore()
        self.env.reset()
        self.env.start()  # get initial state
        segment = 0
        while self.env.state:
            segment += 1
            exp_by_agent = self._collect(ep, segment)
            self.agent.on_experiences(exp_by_agent)
            num_experiences_collected += sum(exp.size for exp in exp_by_agent.values())
        # update the exploration parameters if an episode is finished
        self.agent.exploration_step()

        # performance details
        if self._log_env_summary:
            self.logger.info(f"ep {ep}: {self.env.summary}")

        self.logger.info(
            f"ep {ep} summary - "
            f"running time: {time.time() - t0} "
            f"env steps: {self.env.step_index} "
            f"experiences collected: {num_experiences_collected}"
        )

    def _evaluate(self):
        """Policy evaluation."""
        self.logger.info("Evaluating...")
        self.agent.exploit()
        self.eval_env.reset()
        self.eval_env.start()  # get initial state
        while self.eval_env.state:
            self.eval_env.step(self.agent.choose_action(self.eval_env.state))

        # performance details
        self.logger.info(f"Evaluation result: {self.eval_env.summary}")

    def _collect(self, ep, segment):
        start_step_index = self.env.step_index + 1
        steps_to_go = self._num_steps
        while self.env.state and steps_to_go:
            self.env.step(self.agent.choose_action(self.env.state))
            steps_to_go -= 1

        self.logger.info(
            f"Roll-out finished for ep {ep}, segment {segment}"
            f"(steps {start_step_index} - {self.env.step_index})"
        )

        return self.env.get_experiences()
