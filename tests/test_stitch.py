"""拼稿算法单元测试：接缝筛选、区间换算、标签合并/冲突、最短拼法裁决。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.stitch import (Segment, StitchValidationError, build_merged_text,
                        evaluate_combinations, evaluate_seam,
                        normalize_request, plan_stitch,
                        project_and_merge_labels, segment_offsets)


def seg(index, sid, text, labels, rev=1):
    return Segment(index, sid, rev, text,
                   [dict(l, text=text[l["start"]:l["end"]]) for l in labels])


class TestEvaluateSeam:
    def test_legal_and_illegal_mixed(self):
        legal, rejected = evaluate_seam("甲乙丙丁戊", "丁戊己庚", [1, 2, 3])
        assert legal == [2]
        assert {r.candidate for r in rejected} == {1, 3}

    def test_non_positive_and_too_large(self):
        legal, rejected = evaluate_seam("ab", "ab", [0, 2, 3])
        assert legal == []
        reasons = {r.candidate: r.reason for r in rejected}
        assert "正数" in reasons[0]
        assert "小于两段长度" in reasons[2]

    def test_must_be_smaller_than_each_length(self):
        # k 等于较短段长度即非法
        legal, _ = evaluate_seam("abc", "ab", [2])
        assert legal == []
        legal, _ = evaluate_seam("ab", "abc", [2])
        assert legal == []

    def test_codepoint_not_utf16(self):
        # emoji 单码点：重叠 1 合法
        legal, rejected = evaluate_seam("a😀", "😀b", [1])
        assert legal == [1] and rejected == []

    def test_sorted_output(self):
        # "aaaa"/"aaab"：k=1,2,3 后缀==前缀（全是 a 对 a）；换个真正失配的 k
        legal, rejected = evaluate_seam("aaaa", "aaab", [2, 1, 4])
        assert legal == [1, 2]
        assert [r.candidate for r in rejected] == [4]


class TestOffsetsAndMergedText:
    def test_offsets(self):
        assert segment_offsets([5, 4, 4], [2, 2]) == [0, 3, 5]

    def test_merged_text(self):
        merged = build_merged_text(["甲乙丙丁戊", "丁戊己庚", "己庚辛壬"], [2, 2])
        assert merged == "甲乙丙丁戊己庚辛壬"
        assert len(merged) == 9


class TestLabelProjection:
    def test_projection_without_overlap_labels(self):
        s1 = seg(1, "a", "甲乙丙", [{"id": "x", "start": 0, "end": 1}])
        s2 = seg(2, "b", "丙丁", [{"id": "y", "start": 1, "end": 2}])
        labels, conflicts = project_and_merge_labels(
            [s1, s2], segment_offsets([3, 2], [1]))
        assert conflicts == []
        assert [(l["start"], l["end"]) for l in labels] == [(0, 1), (3, 4)]
        assert labels[0]["sources"] == [
            {"session_index": 1, "session_id": "a", "label_id": "x"}]

    def test_exact_coincidence_merges_sources(self):
        s1 = seg(1, "a", "甲乙丙", [{"id": "x", "start": 1, "end": 3}])
        s2 = seg(2, "b", "乙丙丁", [{"id": "y", "start": 0, "end": 2}])
        labels, conflicts = project_and_merge_labels(
            [s1, s2], segment_offsets([3, 3], [2]))
        assert conflicts == [] and len(labels) == 1
        m = labels[0]
        assert (m["start"], m["end"]) == (1, 3)
        assert [s["label_id"] for s in m["sources"]] == ["x", "y"]
        assert [s["session_index"] for s in m["sources"]] == [1, 2]

    def test_partial_intersection_invalid(self):
        s1 = seg(1, "a", "甲乙丙", [{"id": "x", "start": 1, "end": 3}])  # [1,3)
        s2 = seg(2, "b", "乙丙丁", [{"id": "y", "start": 1, "end": 3}])  # 投影到 [2,4)
        labels, conflicts = project_and_merge_labels(
            [s1, s2], segment_offsets([3, 3], [2]))
        assert labels == [] and len(conflicts) == 1
        assert conflicts[0].a["label_id"] == "x"
        assert conflicts[0].b["label_id"] == "y"

    def test_adjacent_intervals_are_fine(self):
        # 半开区间：end == start 不算相交
        s1 = seg(1, "a", "ab", [{"id": "x", "start": 0, "end": 2}])
        s2 = seg(2, "b", "bc", [{"id": "y", "start": 1, "end": 2}])
        labels, conflicts = project_and_merge_labels(
            [s1, s2], segment_offsets([2, 2], [1]))
        assert conflicts == []
        assert [(l["start"], l["end"]) for l in labels] == [(0, 2), (2, 3)]


class TestPlanStitch:
    def test_unique_shortest_with_merges(self):
        segs = [
            seg(1, "s1", "甲乙丙丁戊", [{"id": "L1", "start": 3, "end": 5}]),
            seg(2, "s2", "丁戊己庚", [{"id": "L1", "start": 0, "end": 2},
                                      {"id": "L2", "start": 2, "end": 4}]),
            seg(3, "s3", "己庚辛壬", [{"id": "L9", "start": 0, "end": 2}]),
        ]
        plan = plan_stitch(segs, [[1, 2, 3], [1, 2]])
        assert plan["status"] == "unique"
        assert plan["overlaps"] == [2, 2]
        result = plan["result"]
        assert result["merged"] == "甲乙丙丁戊己庚辛壬"
        assert [s["start"] for s in result["segments"]] == [0, 3, 5]
        assert len(result["labels"]) == 2
        assert [s["label_id"] for s in result["labels"][0]["sources"]] == ["L1", "L1"]
        assert [s["session_index"] for s in result["labels"][1]["sources"]] == [2, 3]

    def test_tie_requires_equal_overlap_sum_with_different_vectors(self):
        # 合稿长度 = Σ段长 - Σ重叠；并列 ⟺ 重叠总和相同而向量不同。
        # 三段均长 4，两接缝候选各 {2,3}。s1 的 [2,3) 与 s3 的 [0,2)
        # 只在 (3,3)（重叠和最大=6）时部分相交；于是 (2,3) 与 (3,2)
        # （重叠和均为 5）并列最短。
        segs = [
            seg(1, "a", "aaaa", [{"id": "x", "start": 2, "end": 3}]),
            seg(2, "b", "aaaa", []),
            seg(3, "c", "aaaa", [{"id": "y", "start": 0, "end": 2}]),
        ]
        plan = plan_stitch(segs, [[2, 3], [2, 3]])
        assert plan["status"] == "tie"
        assert [t["overlaps"] for t in plan["ties"]] == [[2, 3], [3, 2]]
        assert "result" not in plan

    def test_no_solution_seam(self):
        segs = [seg(1, "a", "abc", []), seg(2, "b", "abd", [])]
        plan = plan_stitch(segs, [[1, 2]])
        assert plan["status"] == "no_solution"
        assert plan["reason"] == "seam_no_legal_candidate"
        assert plan["empty_seams"] == [1]

    def test_no_solution_label_conflict(self):
        segs = [
            seg(1, "a", "甲乙丙", [{"id": "x", "start": 1, "end": 3}]),
            seg(2, "b", "乙丙丁", [{"id": "y", "start": 1, "end": 3}]),
        ]
        plan = plan_stitch(segs, [[2]])
        assert plan["status"] == "no_solution"
        assert plan["reason"] == "label_conflict"
        assert plan["label_conflicts"][0]["label_a"]["label_id"] == "x"

    def test_longer_overlap_gives_shorter_merged_text(self):
        # aaa + aaa：k=1 -> 合稿 5 码点，k=2 -> 4 码点，唯一最短随候选而定
        segs = [seg(1, "a", "aaa", []), seg(2, "b", "aaa", [])]
        plan = plan_stitch(segs, [[1]])
        assert plan["status"] == "unique" and plan["overlaps"] == [1]
        plan2 = plan_stitch(segs, [[2]])
        assert plan2["status"] == "unique" and plan2["overlaps"] == [2]
        assert plan2["merged_length"] < plan["merged_length"]

    def test_overlap_must_be_smaller_than_both_lengths(self):
        # k 等于较短段长度：接缝无合法候选
        segs = [seg(1, "a", "aaa", []), seg(2, "b", "aa", [])]
        plan = plan_stitch(segs, [[2]])
        assert plan["status"] == "no_solution"
        assert plan["empty_seams"] == [1]

    def test_unique_when_lengths_differ(self):
        # "aaa"/"aab"：k=1（a）与 k=2（aa）都合法，取重叠更大的 k=2
        segs = [seg(1, "a", "aaa", []), seg(2, "b", "aab", [])]
        plan = plan_stitch(segs, [[1, 2]])
        assert plan["status"] == "unique"
        assert plan["overlaps"] == [2]
        assert plan["merged_length"] == 4

    def test_rejected_candidates_reported_even_on_success(self):
        segs = [seg(1, "a", "甲乙丙", []), seg(2, "b", "乙丙丁", [])]
        plan = plan_stitch(segs, [[1, 2]])
        assert plan["status"] == "unique"
        assert plan["seam_rejections"][0]["candidate"] == 1


class TestNormalizeRequest:
    def test_basic_ok(self):
        refs, seams = normalize_request(
            [{"session_id": "a", "revision": 1},
             {"session_id": "b", "revision": 2}],
            [{"candidates": [1, 2]}])
        assert refs[0]["session_id"] == "a" and seams == [[1, 2]]

    def test_segment_count_bounds(self):
        with pytest.raises(StitchValidationError):
            normalize_request([{"session_id": "a", "revision": 1}], [])
        with pytest.raises(StitchValidationError):
            normalize_request(
                [{"session_id": str(i), "revision": 1} for i in range(7)],
                [{"candidates": [1]}] * 6)

    def test_duplicate_session_rejected(self):
        with pytest.raises(StitchValidationError, match="重复"):
            normalize_request(
                [{"session_id": "a", "revision": 1},
                 {"session_id": "a", "revision": 1}],
                [{"candidates": [1]}])

    def test_seam_count_must_match(self):
        with pytest.raises(StitchValidationError, match="接缝"):
            normalize_request(
                [{"session_id": "a", "revision": 1},
                 {"session_id": "b", "revision": 1},
                 {"session_id": "c", "revision": 1}],
                [{"candidates": [1]}])

    def test_candidate_count_bounds_and_dupes(self):
        base = [{"session_id": "a", "revision": 1},
                {"session_id": "b", "revision": 1}]
        with pytest.raises(StitchValidationError):
            normalize_request(base, [{"candidates": []}])
        with pytest.raises(StitchValidationError):
            normalize_request(base, [{"candidates": [1, 2, 3, 4]}])
        with pytest.raises(StitchValidationError, match="重复"):
            normalize_request(base, [{"candidates": [1, 1]}])

    def test_bool_not_accepted_as_int(self):
        with pytest.raises(StitchValidationError):
            normalize_request(
                [{"session_id": "a", "revision": 1},
                 {"session_id": "b", "revision": 1}],
                [{"candidates": [True]}])

    def test_evaluate_combinations_helper(self):
        segs = [seg(1, "a", "ab", []), seg(2, "b", "bc", [])]
        combos = evaluate_combinations(segs, [[1]])
        assert len(combos) == 1 and combos[0].legal
        assert combos[0].merged_length == 3
