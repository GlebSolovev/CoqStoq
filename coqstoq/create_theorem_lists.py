"""
Creates a (shuffled) list of theorems referencing theorems created by 
`find_eval_theorems`.
"""

from __future__ import annotations
import os
from typing import Any

import json
import argparse
import random
import re
from pathlib import Path

from dataclasses import dataclass, asdict
from coqstoq.eval_thms import Split, EvalTheorem

from coqstoq.predefined_projects import (
    PREDEFINED_PROJECTS,
    TEST_SPLIT,
    VAL_SPLIT,
    CUTOFF_SPLIT,
)


@dataclass(unsafe_hash=True)
class TheoremReference:
    thm_path: Path
    thm_idx: int

    def to_json(self):
        return {"thm_path": str(self.thm_path), "thm_idx": self.thm_idx}

    def to_eval_thm(self) -> EvalTheorem:
        with (Path.cwd() / self.thm_path).open("r") as fin:
            thms = json.load(fin)
            return EvalTheorem.from_json(thms[self.thm_idx])

    @classmethod
    def from_json(cls, data: Any) -> TheoremReference:
        return cls(Path(data["thm_path"]), data["thm_idx"])


@dataclass
class TargetTheorem:
    name: str
    start_pos: int
    added_to_split: bool = False


def load_reference_list(split: Split, coqstoq_loc: Path) -> list[TheoremReference]:
    theorem_list_loc = coqstoq_loc / split.theorem_list_loc
    with theorem_list_loc.open("r") as fin:
        return [TheoremReference.from_json(thm) for thm in json.load(fin)]


def create_split_list(split: Split, seed: int, target_thms_by_files: dict[str, list[TargetTheorem]]) -> list[TheoremReference]:
    # Convert thm_path from "coqpilot-theorems/.../file.json" to ".../file.v"
    def normalize_thm_path(p: Path) -> str:
        normalized = str(p)
        if normalized.startswith("coqpilot-theorems/"):
            normalized = normalized[len("coqpilot-theorems/"):]
        normalized = normalized.replace(".json", ".v")
        return normalized

    split_theorems_loc = Path.cwd() / split.thm_dir_name
    assert split_theorems_loc.exists()
    theorem_list: list[TheoremReference] = []
    for thm_file_loc in split_theorems_loc.glob("**/*.json"):
        assert thm_file_loc.is_relative_to(Path.cwd())
        rel_thm_file_loc = thm_file_loc.relative_to(Path.cwd())

        thm_file = normalize_thm_path(rel_thm_file_loc)
        target_thms = target_thms_by_files.get(thm_file)
        if target_thms is None:
            continue

        with thm_file_loc.open("r") as fin:
            thms = json.load(fin)
            for idx, thm in enumerate(thms):
                thm_star_pos = int(thm["theorem_start_pos"]["line"])
                # add `thm` to the split only if there is such target one
                for target_thm in target_thms:
                    # yes, for some reason Rango generates lines numbers shifted by 1
                    if target_thm.start_pos - 1 == thm_star_pos:
                        assert not target_thm.added_to_split
                        theorem_list.append(
                            TheoremReference(rel_thm_file_loc, idx))
                        target_thm.added_to_split = True

    # Check all target theorems are added to split
    all_target_theorems_found = True
    for _, target_thms in target_thms_by_files.items():
        for target_thm in target_thms:
            if not target_thm.added_to_split:
                all_target_theorems_found = False
                print(
                    f"Target theorem {target_thm.name} is missing in the split")

    if not all_target_theorems_found:
        raise ValueError(
            "Failed to create a split: some target theorems were not found")

    print("All target theorems were added to the split")

    # Do not shuffle target split, better to sort as it is in the target input

    file_order = {file_path: i for i,
                  file_path in enumerate(target_thms_by_files.keys())}

    def sort_key(thm_ref: TheoremReference):
        normalized = normalize_thm_path(thm_ref.thm_path)
        # Get its order index, or large number if not found
        return file_order.get(normalized, float('inf'))

    theorem_list.sort(key=sort_key)

    # random.seed(seed)
    # random.shuffle(theorem_list)

    return theorem_list


def load_target_theorems(targets_dir: Path) -> dict[str, list[TargetTheorem]]:
    result: dict[str, list[TargetTheorem]] = {}

    for json_file in targets_dir.glob("*.json"):
        with json_file.open("r") as f:
            data: dict[str, list[str]] = json.load(f)

        for file_path, theorem_names in data.items():
            if file_path not in result:
                result[file_path] = []
            for name in theorem_names:
                # Put mock `start_pos` currently
                result[file_path].append(
                    TargetTheorem(name=name, start_pos=-1)
                )
    print(f"Targets have been loaded from '{targets_dir}'")

    return result


def resolve_start_positions(result: dict[str, list[TargetTheorem]], repos_dir: Path) -> None:
    for file_path, theorems in result.items():
        abs_path = repos_dir / file_path  # full path to the .v file
        if not abs_path.exists():
            raise ValueError(f"Target file '{abs_path}' not found")

        with abs_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        for theorem in theorems:
            pattern = rf"(?<![A-Za-z0-9_']){re.escape(theorem.name)}(?![A-Za-z0-9_'])"
            start_pos = -1
            for i, line in enumerate(lines, start=1):  # lines start at 1
                if re.search(pattern, line):
                    start_pos = i
                    break
            if start_pos == -1:
                raise ValueError(
                    f"Target theorem '{theorem.name}' is not found in '{abs_path}'")
            theorem.start_pos = start_pos
    print(
        f"Input targets for the workspace '{repos_dir}' have been resolved"
    )


def save_resolved_targets(result: dict[str, list[TargetTheorem]], targets_dir: Path):
    def build_resolved_path() -> Path:
        parent = targets_dir.parent
        new_dir_name = f"{targets_dir.name}-resolved"
        return parent / new_dir_name / "all_target_theorems.json"

    output_file = build_resolved_path()
    os.makedirs(output_file.parent, exist_ok=True)

    serializable_data = {
        file_path: [asdict(theorem) for theorem in theorems]
        for file_path, theorems in result.items()
    }

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(serializable_data, f, indent=2)


def create_theorem_list(seed: int, split_name: str, targets_dir: Path):
    split = Split.from_name(split_name)

    target_thms_by_files = load_target_theorems(targets_dir)
    resolve_start_positions(target_thms_by_files, Path(split.dir_name))
    save_resolved_targets(target_thms_by_files, targets_dir)

    thm_list = create_split_list(split, seed, target_thms_by_files)
    with open(split.theorem_list_loc, "w") as fout:
        json.dump([thm.to_json() for thm in thm_list], fout, indent=2)

    print(f"Successfully created the split at: '{split.theorem_list_loc}'")


if __name__ == "__main__":
    SEED = 0
    parser = argparse.ArgumentParser(
        description="Create a list of theorems for the given split."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=SEED,
        help="Seed for shuffling the theorem list.",
    )
    parser.add_argument(
        "split_name",
        type=str,
        help="Name of the split to create a theorem list for.",
    )
    parser.add_argument(
        "targets_dir",
        type=str,
        help="Path of the directory containing JSON files describing target theorems to include in the split.",
    )

    args = parser.parse_args()

    create_theorem_list(SEED, args.split_name, Path(args.targets_dir))
