import csv
from pathlib import Path

import pytest

from opencode_tracker.csv_merger import merge_csvs


HEADERS = ["date", "model", "person", "cost", "billing_source", "allowance", "list_cost"]


def write_csv(path: Path, rows, headers=HEADERS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def test_dedupes_overlap_across_files(tmp_path):
    a = write_csv(tmp_path / "usage_7d.csv", [
        ["2026-10-26", "kimi-k3", "alice", "0.00", "go", "0.50", "0.50"],
    ])
    b = write_csv(tmp_path / "usage_30d.csv", [
        ["2026-10-26", "kimi-k3", "alice", "0.00", "go", "0.50", "0.50"],
        ["2026-10-20", "kimi-k3", "alice", "0.00", "go", "0.25", "0.25"],
    ])

    result = merge_csvs([str(a), str(b)], verbose=False)

    assert result["rows_before_dedupe"] == 3
    assert result["rows_after_dedupe"] == 2
    assert result["dupes_removed"] == 1
    assert result["total_allowance"] == pytest.approx(0.75)


def test_keeps_distinct_rows_inside_one_file(tmp_path):
    a = write_csv(tmp_path / "one.csv", [
        ["2026-10-26", "kimi-k3", "alice", "0.00", "go", "0.50", "0.50"],
        ["2026-10-26", "kimi-k3", "alice", "0.00", "go", "0.50", "0.50"],
    ])

    result = merge_csvs([str(a)], verbose=False)

    # Same key twice in the same export: both rows are real and must survive.
    assert result["rows_after_dedupe"] == 2
    assert result["dupes_removed"] == 0


def test_billable_only_totals_match_visible_rows(tmp_path):
    a = write_csv(tmp_path / "mix.csv", [
        ["2026-10-26", "claude-haiku-5-5", "bob", "1.20", "balance", "0.00", "1.20"],
        ["2026-10-26", "kimi-k3", "alice", "0.00", "go", "0.50", "0.50"],
        ["2026-10-26", "muse-spark-1.2-contributor", "alice", "0.00", "free", "0.00", "0.00"],
    ])

    result = merge_csvs([str(a)], billable_only=True, verbose=False)

    assert result["rows_filtered"] == 1
    assert result["total_cost"] == pytest.approx(1.20)
    assert result["total_billable"] == pytest.approx(1.20)
    assert result["total_allowance"] == pytest.approx(0.0)
    # Context figures still describe every source.
    assert result["total_cost_all"] == pytest.approx(1.20)
    assert result["total_allowance_all"] == pytest.approx(0.50)
    assert [m["model"] for m in result["per_model"]] == ["claude-haiku-5-5"]


def test_plan_windows_and_pct(tmp_path):
    a = write_csv(tmp_path / "go.csv", [
        ["2026-11-10", "kimi-k3", "alice", "0.00", "go", "3.00", "3.00"],
    ])

    result = merge_csvs([str(a)], plan="go", verbose=False)
    row = result["per_model"][0]

    assert row["monthly_limit"] == 15
    assert row["pct_month"] == pytest.approx(20.0)
    assert row["5h_limit"] == pytest.approx(3.0)
    assert row["weekly_limit"] == pytest.approx(7.5)


def test_detects_alias_headers_and_returns_detailed_rows(tmp_path):
    a = write_csv(
        tmp_path / "aliases.csv",
        [["2026-10-05", "glm-5.3", "alice", "0.80", "balance", "0.00", "0.80"]],
        headers=["Day", "Model ID", "User", "Amount", "Billing Source", "Go Allowance", "List Price"],
    )

    result = merge_csvs([str(a)], verbose=False)

    assert result["per_model"][0]["model"] == "glm-5.3"
    assert result["total_cost"] == pytest.approx(0.80)
    assert len(result["detailed_rows"]) == 1
    assert result["detailed_rows"][0]["person"] == "alice"


def test_no_files_raises():
    with pytest.raises(FileNotFoundError):
        merge_csvs(["does-not-exist/*.csv"], verbose=False)
