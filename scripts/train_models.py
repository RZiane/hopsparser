import itertools
import multiprocessing
import os.path
import pathlib
import subprocess
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

import click
import click_pathlib

from npdependency import conll2018_eval as evaluator


class TrainResults(NamedTuple):
    dev_upos: float
    dev_las: float
    test_upos: float
    test_las: float


def train_single_model(
    train_file: pathlib.Path,
    dev_file: pathlib.Path,
    pred_file: pathlib.Path,
    out_dir: pathlib.Path,
    config_path: pathlib.Path,
    device: str,
    additional_args: Dict[str, str],
) -> TrainResults:
    subprocess.run(
        [
            "graph_parser",
            "--train_file",
            str(train_file),
            "--dev_file",
            str(dev_file),
            "--pred_file",
            str(pred_file),
            "--out_dir",
            str(out_dir),
            "--device",
            device,
            *(
                a
                for key, value in additional_args.items()
                if value
                for a in (f"--{key}", value)
            ),
            str(config_path),
        ],
        check=True,
    )

    gold_devset = evaluator.load_conllu_file(dev_file)
    syst_devset = evaluator.load_conllu_file(out_dir / f"{dev_file.name}.parsed")
    dev_metrics = evaluator.evaluate(gold_devset, syst_devset)

    gold_testset = evaluator.load_conllu_file(pred_file)
    syst_testset = evaluator.load_conllu_file(out_dir / f"{pred_file.name}.parsed")
    test_metrics = evaluator.evaluate(gold_testset, syst_testset)

    return TrainResults(
        dev_upos=dev_metrics["UPOS"].f1,
        dev_las=dev_metrics["LAS"].f1,
        test_upos=test_metrics["UPOS"].f1,
        test_las=test_metrics["LAS"].f1,
    )


# It would be nice to be able to have this as a closure, but unfortunately it doesn't work since
# closures are not picklable and multiprocessing can only deal with picklable workers
def worker(device_queue, name, kwargs) -> Tuple[str, TrainResults]:
    # We use no more workers than devices so the queue should never be empty when launching the
    # worker fun so we want to fail early here if the Queue is empty. It does not feel right but it
    # works.
    device = device_queue.get(block=False)
    kwargs["device"] = device
    print(f"Start training {name} on {device}")
    res = train_single_model(**kwargs)
    device_queue.put(device)
    print(f"Run {name} finished with results {res}")
    return (name, res)


def run_multi(
    runs: Iterable[Tuple[str, Dict[str, Any]]], devices: List[str]
) -> List[Tuple[str, TrainResults]]:
    with multiprocessing.Manager() as manager:
        device_queue = manager.Queue()
        for d in devices:
            device_queue.put(d)

        with multiprocessing.Pool(len(devices)) as pool:
            res_future = pool.starmap_async(
                worker,
                ((device_queue, *r) for r in runs),
            )
            res = res_future.get()
    return res


def parse_args_callback(
    _ctx: click.Context,
    _opt: Union[click.Argument, click.Option],
    val: Optional[List[str]],
) -> Optional[List[Tuple[str, List[str]]]]:
    if val is None:
        return None
    res: List[Tuple[str, List[str]]] = []
    for v in val:
        name, values = v.split("=", maxsplit=1)
        res.append((name, values.split(",")))
    return res


# TODO: add multitrials mode, options to report stats and random seed tuning (keeping the best out
# of n models…)
@click.command()
@click.argument(
    "configs_dir",
    type=click_pathlib.Path(resolve_path=True, exists=True, file_okay=False),
)
@click.argument(
    "treebanks_dir",
    type=click_pathlib.Path(resolve_path=True, exists=True, file_okay=False),
)
@click.option(
    "--args",
    multiple=True,
    callback=parse_args_callback,
    help=(
        "An additional list of values for an argument, given as `name=value,value2,…`."
        " Leave a trailing comma to also run the default value of the argument"
        " Can be provided several times for different arguments."
        " Path options should have different file names."
    ),
)
@click.option(
    "--devices",
    default="cpu",
    callback=(lambda _ctx, _opt, val: val.split(",")),
    help="A comma-separated list of devices to run on.",
)
@click.option(
    "--out-dir",
    default=".",
    type=click_pathlib.Path(resolve_path=True, exists=False, file_okay=False),
)
@click.option("--prefix", default="", help="A custom prefix to prepend to run names.")
@click.option(
    "--rand-seeds",
    callback=(
        lambda _ctx, _opt, val: None
        if val is None
        else [int(v) for v in val.split(",")]
    ),
    help=(
        "A comma-separated list of random seeds to try and run stats on."
        " Only the seed with the best result will be kept for every running config."
    ),
)
def main(
    args: Optional[List[Tuple[str, List[str]]]],
    configs_dir: pathlib.Path,
    devices: List[str],
    out_dir: pathlib.Path,
    prefix: str,
    rand_seeds: Optional[List[int]],
    treebanks_dir: pathlib.Path,
):
    out_dir.mkdir(parents=True, exist_ok=True)
    treebanks = [train.parent for train in treebanks_dir.glob("**/train.conllu")]
    configs = list(configs_dir.glob("*.yaml"))
    if rand_seeds is not None:
        args = [
            ("rand_seed", [str(s) for s in rand_seeds]),
            *(args if args is not None else []),
        ]
    additional_args_combinations: List[Dict[str, str]]
    if args:
        args_names, all_args_values = map(list, zip(*args))
        additional_args_combinations = [
            dict(zip(args_names, args_values))
            for args_values in itertools.product(*all_args_values)
        ]
    else:
        additional_args_combinations = [{}]
    runs: List[Tuple[str, Dict[str, Any]]] = []
    for t in treebanks:
        for c in configs:
            common_params = {
                "train_file": t / "train.conllu",
                "dev_file": t / "dev.conllu",
                "pred_file": t / "test.conllu",
                "config_path": c,
            }
            run_base_name = f"{prefix}{t.name}-{c.stem}"
            run_out_root_dir = out_dir / run_base_name
            for additional_args in additional_args_combinations:
                if not additional_args:
                    run_out_dir = run_out_root_dir
                    run_name = run_base_name
                else:
                    args_combination_str = "+".join(
                        f"{n}={os.path.basename(v)}" if v else f"no{n}"
                        for n, v in additional_args.items()
                    )
                    run_out_dir = run_out_root_dir / args_combination_str
                    run_name = f"{run_base_name}+{args_combination_str}"
                if run_out_dir.exists():
                    print(f"{run_out_dir} already exists, skipping this run.")
                    continue
                runs.append(
                    (
                        run_name,
                        {
                            **common_params,
                            "additional_args": additional_args,
                            "out_dir": run_out_dir,
                        },
                    ),
                )

    res = run_multi(runs, devices)

    summary_file = out_dir / "summary.tsv"
    if not summary_file.exists():
        summary_file.write_text("run\tdev UPOS\tdev LAS\ttest UPOS\ttest LAS\n")
    with open(summary_file, "a") as out_stream:
        for name, scores in res:
            out_stream.write(
                f"{name}\t{100*scores.dev_upos:.2f}\t{100*scores.dev_las:.2f}\t{100*scores.test_upos:.2f}\t{100*scores.test_las:.2f}\n"
            )
    # TODO: stats aggregated across random seeds computed here


if __name__ == "__main__":
    main()
