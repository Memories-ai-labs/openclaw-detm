"""Run OSWorld benchmark with DETM agent — multi-environment parallel version.

Spawns N Docker containers in parallel, each running tasks from a shared queue.

Usage:
    cd /home/alex/OSWorld
    export $(grep -v '^#' /home/alex/openclaw-memoriesai/.env | xargs)
    sg docker -c "PYTHONPATH=/home/alex/openclaw-memoriesai/src:/home/alex/openclaw-memoriesai \
      .venv/bin/python3 /home/alex/openclaw-memoriesai/benchmarks/osworld/run_detm_multienv.py \
      --provider_name docker --max_steps 15 --num_envs 4"

Resume support: automatically skips tasks that already have result.txt.
"""

import argparse
import datetime
import json
import logging
import os
import signal
import sys
import time
from multiprocessing import Manager, Process, current_process

from tqdm import tqdm

OSWORLD_DIR = os.environ.get("OSWORLD_DIR", "/home/alex/OSWorld")
sys.path.insert(0, OSWORLD_DIR)

from desktop_env.desktop_env import DesktopEnv

logger = logging.getLogger("desktopenv.experiment")
processes = []
is_terminating = False


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    dt = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
    log_dir = os.path.join(OSWORLD_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    handlers = [
        (logging.FileHandler(os.path.join(log_dir, f"detm-multi-{dt}.log"), encoding="utf-8"), logging.INFO),
        (logging.FileHandler(os.path.join(log_dir, f"detm-multi-debug-{dt}.log"), encoding="utf-8"), logging.DEBUG),
        (logging.StreamHandler(sys.stdout), logging.INFO),
    ]
    fmt = logging.Formatter("[%(asctime)s %(levelname)s %(module)s/%(lineno)d-%(processName)s] %(message)s")
    for h, level in handlers:
        h.setLevel(level)
        h.setFormatter(fmt)
        root.addHandler(h)


def config():
    parser = argparse.ArgumentParser(description="Run OSWorld with DETM agent (parallel)")
    parser.add_argument("--provider_name", type=str, default="docker")
    parser.add_argument("--path_to_vm", type=str, default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--screen_width", type=int, default=1920)
    parser.add_argument("--screen_height", type=int, default=1080)
    parser.add_argument("--sleep_after_execution", type=float, default=0.0)
    parser.add_argument("--max_steps", type=int, default=15)
    parser.add_argument("--domain", type=str, default="all")
    parser.add_argument("--test_config_base_dir", type=str,
                        default=os.path.join(OSWORLD_DIR, "evaluation_examples"))
    parser.add_argument("--test_all_meta_path", type=str,
                        default=os.path.join(OSWORLD_DIR, "evaluation_examples/test_all.json"))
    parser.add_argument("--result_dir", type=str, default=os.path.join(OSWORLD_DIR, "results"))
    parser.add_argument("--num_envs", type=int, default=1, help="Number of parallel Docker environments")
    return parser.parse_args()


def get_unfinished(result_dir, model_name, test_all_meta):
    """Filter out tasks that already have result.txt."""
    target_dir = os.path.join(result_dir, "pyautogui", "screenshot", model_name)
    if not os.path.exists(target_dir):
        return test_all_meta

    finished = set()
    for domain in os.listdir(target_dir):
        domain_path = os.path.join(target_dir, domain)
        if not os.path.isdir(domain_path):
            continue
        for example_id in os.listdir(domain_path):
            example_path = os.path.join(domain_path, example_id)
            if os.path.isdir(example_path) and "result.txt" in os.listdir(example_path):
                finished.add(example_id)

    filtered = {}
    for domain, examples in test_all_meta.items():
        remaining = [e for e in examples if e not in finished]
        if remaining:
            filtered[domain] = remaining
    return filtered


_DOMAIN_WAIT = {
    "chrome": 15,
    "gimp": 25,
    "libreoffice_calc": 50,
    "libreoffice_impress": 50,
    "libreoffice_writer": 50,
    "multi_apps": 40,
    "os": 15,
    "thunderbird": 25,
    "vlc": 20,
    "vs_code": 20,
}


def run_single(agent, env, example, max_steps, instruction, sleep_after, example_result_dir, domain=None):
    """Run one task. Returns score (float)."""
    env.reset(task_config=example)
    agent.reset(vm_ip=env.vm_ip)

    wait = _DOMAIN_WAIT.get(domain, 40)
    logger.info("[%s] Waiting %ds for %s environment...", current_process().name, wait, domain or "unknown")
    time.sleep(wait)
    obs = env._get_obs()
    done = False
    step_idx = 0

    env.controller.start_recording()

    # Checkpoints: evaluate at these step counts to allow fair comparison
    EVAL_CHECKPOINTS = [15, 50]

    while not done and step_idx < max_steps:
        # Save screenshot BEFORE action (what the agent sees when deciding)
        ts = datetime.datetime.now().strftime("%Y%m%d@%H%M%S%f")
        with open(os.path.join(example_result_dir, f"step_{step_idx+1}_before_{ts}.png"), "wb") as f:
            f.write(obs["screenshot"])

        response, actions = agent.predict(instruction, obs)
        debug = getattr(agent, "_last_debug", {})

        for action in actions:
            ts = datetime.datetime.now().strftime("%Y%m%d@%H%M%S%f")
            logger.info("[%s] Step %d: %s (tool=%s thought=%s)",
                        current_process().name, step_idx + 1, action,
                        debug.get("tool", ""), debug.get("thought", "")[:100])

            traj_entry = {
                "step_num": step_idx + 1,
                "action_timestamp": ts,
                "action": action,
                "tool": debug.get("tool"),
                "tool_args": debug.get("args", {}),
                "thought": debug.get("thought", ""),
                "tool_result": debug.get("result", ""),
            }

            # Handle sentinel values — pass through to env.step() so
            # action_history is populated (needed for infeasible evaluation)
            if action in ("DONE", "FAIL", "WAIT"):
                with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                    f.write(json.dumps(traj_entry))
                    f.write("\n")
                obs, reward, done, info = env.step(action, sleep_after)
                logger.info("[%s] Agent signaled %s.", current_process().name, action)
                break

            obs, reward, done, info = env.step(action, sleep_after)
            logger.info("[%s] Reward: %.2f, Done: %s", current_process().name, reward, done)

            traj_entry["reward"] = reward
            traj_entry["done"] = done
            traj_entry["info"] = info

            # Save screenshot AFTER action (result of the action)
            with open(os.path.join(example_result_dir, f"step_{step_idx+1}_after_{ts}.png"), "wb") as f:
                f.write(obs["screenshot"])

            with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                f.write(json.dumps(traj_entry))
                f.write("\n")

            if done:
                logger.info("[%s] Episode done.", current_process().name)
                break

        step_idx += 1

        # Checkpoint evaluation at intermediate step counts
        if not done and step_idx in EVAL_CHECKPOINTS:
            try:
                time.sleep(3)
                ckpt_result = env.evaluate()
                ckpt_file = os.path.join(example_result_dir, f"result_at_{step_idx}.txt")
                with open(ckpt_file, "w") as f:
                    f.write(f"{ckpt_result}\n")
                logger.info("[%s] Checkpoint @%d steps: %.2f", current_process().name, step_idx, ckpt_result)
            except Exception as e:
                logger.warning("[%s] Checkpoint eval failed @%d: %s", current_process().name, step_idx, e)

    time.sleep(5)  # Settle
    result = env.evaluate()
    logger.info("[%s] Result: %.2f", current_process().name, result)

    with open(os.path.join(example_result_dir, "result.txt"), "w") as f:
        f.write(f"{result}\n")

    env.controller.end_recording(os.path.join(example_result_dir, "recording.mp4"))
    return result


def worker(task_queue, args, shared_scores):
    """Worker process: creates its own DesktopEnv + DETMAgent, pulls tasks from queue."""
    env = None
    try:
        from benchmarks.osworld.detm_agent import DETMAgent
        agent = DETMAgent()

        _sup = os.environ.get("ACU_OPENROUTER_LIVE_MODEL", "gemini-flash")
        _sup_short = _sup.rsplit("/", 1)[-1]
        MODEL_NAME = f"detm-{_sup_short}-uitars"

        env = DesktopEnv(
            provider_name=args.provider_name,
            path_to_vm=args.path_to_vm,
            action_space="pyautogui",
            screen_size=(args.screen_width, args.screen_height),
            headless=args.headless,
            os_type="Ubuntu",
            require_a11y_tree=False,
        )

        logger.info("[%s] Environment ready.", current_process().name)

        while True:
            try:
                item = task_queue.get(timeout=5)
            except Exception:
                break

            domain, example_id = item
            config_file = os.path.join(
                args.test_config_base_dir, f"examples/{domain}/{example_id}.json"
            )
            with open(config_file) as f:
                example = json.load(f)

            instruction = example["instruction"]
            logger.info("[%s] [Domain]: %s  [ID]: %s", current_process().name, domain, example_id)
            logger.info("[%s] [Instruction]: %s", current_process().name, instruction)

            example_result_dir = os.path.join(
                args.result_dir, "pyautogui", "screenshot", MODEL_NAME, domain, example_id
            )
            os.makedirs(example_result_dir, exist_ok=True)

            try:
                score = run_single(
                    agent, env, example, args.max_steps, instruction,
                    args.sleep_after_execution, example_result_dir, domain=domain,
                )
                shared_scores.append(score)
            except Exception as e:
                logger.error("[%s] Exception in %s/%s: %s",
                             current_process().name, domain, example_id, e, exc_info=True)
                with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                    f.write(json.dumps({"Error": str(e)}))
                    f.write("\n")

    except Exception as e:
        logger.error("[%s] Process-level error: %s", current_process().name, e, exc_info=True)
    finally:
        if env:
            try:
                env.close()
                logger.info("[%s] Environment closed.", current_process().name)
            except Exception as e:
                logger.error("[%s] Error closing env: %s", current_process().name, e)


def signal_handler(signum, frame):
    global is_terminating, processes
    if is_terminating:
        return
    is_terminating = True
    logger.info("Received signal %d. Shutting down...", signum)
    for p in processes:
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(2)
    for p in processes:
        if p.is_alive():
            try:
                os.kill(p.pid, signal.SIGKILL)
            except Exception:
                pass
    sys.exit(0)


def main():
    global processes
    setup_logging()
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    args = config()
    _sup = os.environ.get("ACU_OPENROUTER_LIVE_MODEL", "gemini-flash")
    _sup_short = _sup.rsplit("/", 1)[-1]
    MODEL_NAME = f"detm-{_sup_short}-uitars"

    with open(args.test_all_meta_path) as f:
        test_all_meta = json.load(f)

    if args.domain != "all":
        test_all_meta = {args.domain: test_all_meta[args.domain]}

    test_all_meta = get_unfinished(args.result_dir, MODEL_NAME, test_all_meta)
    total = sum(len(v) for v in test_all_meta.values())
    logger.info("Tasks remaining: %d", total)
    for domain, examples in test_all_meta.items():
        logger.info("  %s: %d", domain, len(examples))

    if total == 0:
        logger.info("All tasks complete.")
        return

    with Manager() as manager:
        shared_scores = manager.list()
        task_queue = manager.Queue()

        # Flatten and enqueue all tasks
        for domain, examples in test_all_meta.items():
            for example_id in examples:
                task_queue.put((domain, example_id))

        num_envs = min(args.num_envs, total)
        logger.info("Starting %d parallel environments for %d tasks.", num_envs, total)

        processes = []
        for i in range(num_envs):
            p = Process(
                target=worker,
                args=(task_queue, args, shared_scores),
                name=f"DetmWorker-{i+1}",
            )
            p.daemon = True
            p.start()
            processes.append(p)
            logger.info("Started %s (PID %d)", p.name, p.pid)

        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down.")
            signal_handler(signal.SIGINT, None)

        scores = list(shared_scores)

    if scores:
        logger.info("Final score: %.1f%% (%d/%d)",
                     sum(scores) / len(scores) * 100, int(sum(scores)), len(scores))


if __name__ == "__main__":
    main()
