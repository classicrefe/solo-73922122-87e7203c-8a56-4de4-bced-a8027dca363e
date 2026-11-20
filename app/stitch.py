"""拼稿核心算法（纯函数，不碰数据库与 HTTP）。

场景：档案分段扫描后，前一段末尾可能在后一段开头重现。用户按阅读顺序
给出 2~6 个已确认会话（其确认结果即“段”）以及每道接缝的 1~3 个候选
重叠码点数，本模块负责：

1. 逐道接缝筛掉非法候选（非正数、不小于任一段长度、或前后重叠区逐码点
   不一致），并记录被拒原因；
2. 枚举全部合法候选组合（每道接缝取一个重叠长度）；
3. 对每个组合把各段的标签实体换算到合稿的 Unicode 码点半开区间，
   完全重合的标签合并（记下各自的会话号与标签号），任何其他相交都使
   该组合无效；
4. 选出合稿码点总数最小的合法拼法：唯一最短才产出结果，并列最短时
   返回两组以上重叠长度供复核，无合法拼法时返回原因。

约定：Python str 的索引即 Unicode 码点索引；区间一律为左闭右开 [start, end)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Sequence

MIN_SEGMENTS = 2
MAX_SEGMENTS = 6
MIN_CANDIDATES = 1
MAX_CANDIDATES = 3


class StitchValidationError(ValueError):
    """拼稿请求本身的结构/取值非法（面向用户的中文信息）。"""


@dataclass
class Segment:
    """一段：来自某个会话确认结果的文本与其标签。"""
    session_index: int          # 1 基的阅读顺序号
    session_id: str
    revision: int
    text: str
    labels: list[dict]          # [{id, start, end, text}]，区间相对本段


@dataclass
class SeamReject:
    seam_index: int             # 1 基接缝号（第 i 段 与 第 i+1 段之间）
    candidate: int
    reason: str


@dataclass
class LabelConflict:
    """部分相交（非完全重合）的一对标签来源。"""
    a: dict
    b: dict
    interval: tuple[int, int]


@dataclass
class Combination:
    overlaps: list[int]                 # 各接缝选用的重叠长度
    merged_length: int
    legal: bool
    conflicts: list[LabelConflict] = field(default_factory=list)


def evaluate_seam(prev_text: str, next_text: str,
                  candidates: Sequence[int]) -> tuple[list[int], list[SeamReject]]:
    """筛一道接缝的合法候选。

    合法条件（须同时满足）：
    - 0 < k < len(prev_text) 且 0 < k < len(next_text)；
    - prev_text[-k:] == next_text[:k]（前段末尾与后段开头逐码点相同）。
    返回 (合法候选升序去重, 被拒候选及原因)。
    """
    legal: list[int] = []
    rejected: list[SeamReject] = []
    seen: set[int] = set()
    for k in candidates:
        if k in seen:
            rejected.append(SeamReject(0, k, "候选重叠长度重复"))
            continue
        seen.add(k)
        if k <= 0:
            rejected.append(SeamReject(0, k, "重叠码点数必须是正数"))
        elif k >= len(prev_text) or k >= len(next_text):
            rejected.append(SeamReject(0, k,
                f"重叠码点数 {k} 必须同时小于两段长度 "
                f"({len(prev_text)}、{len(next_text)})"))
        elif prev_text[-k:] != next_text[:k]:
            rejected.append(SeamReject(0, k,
                f"前段末尾 {k} 码点与后段开头 {k} 码点不完全相同"))
        else:
            legal.append(k)
    legal.sort()
    return legal, rejected


def segment_offsets(lengths: Sequence[int], overlaps: Sequence[int]) -> list[int]:
    """给定各段长度与各接缝重叠长度，求每段在合稿中的起点码点偏移。"""
    offsets = [0]
    for i, k in enumerate(overlaps):
        offsets.append(offsets[-1] + lengths[i] - k)
    return offsets


def build_merged_text(texts: Sequence[str], overlaps: Sequence[int]) -> str:
    """拼接合稿：第一段全量，之后每段只追加去掉重叠前缀的后缀。"""
    parts = [texts[0]]
    for i, k in enumerate(overlaps, start=1):
        parts.append(texts[i][k:])
    return "".join(parts)


def project_and_merge_labels(segments: Sequence[Segment],
                             offsets: Sequence[int]
                             ) -> tuple[list[dict], list[LabelConflict]]:
    """把各段标签换算到合稿区间；完全重合的合并，其他相交记为冲突。

    返回 (合并后的标签列表（按起点、终点升序）, 冲突列表)。
    合并标签形如：
      {id, start, end, text, sources: [{session_index, session_id, label_id}]}
    单来源标签的 sources 长度为 1。
    """
    flat: list[dict] = []
    for seg, offset in zip(segments, offsets):
        for lab in seg.labels:
            start = offset + lab["start"]
            end = offset + lab["end"]
            flat.append({
                "start": start,
                "end": end,
                "text": lab.get("text", seg.text[lab["start"]:lab["end"]]),
                "source": {
                    "session_index": seg.session_index,
                    "session_id": seg.session_id,
                    "label_id": lab["id"],
                },
            })
    flat.sort(key=lambda x: (x["start"], x["end"]))

    conflicts: list[LabelConflict] = []
    merged: list[dict] = []
    for item in flat:
        if merged and item["start"] < merged[-1]["end"]:
            # 与前一个区间相交：半开区间下 start < end_prev 即相交
            prev = merged[-1]
            if item["start"] == prev["start"] and item["end"] == prev["end"]:
                # 完全重合：合并来源
                prev["sources"].append(item["source"])
            else:
                conflicts.append(LabelConflict(
                    a=prev["sources"][0], b=item["source"],
                    interval=(item["start"], min(item["end"], prev["end"]))))
                # 冲突项仍保留在序列里，继续参与后续判定，保证不漏报
                merged.append({
                    "start": item["start"], "end": item["end"],
                    "text": item["text"], "sources": [item["source"]]})
        else:
            merged.append({
                "start": item["start"], "end": item["end"],
                "text": item["text"], "sources": [item["source"]]})

    # 清理：冲突时该组合整体作废，merged 仅用于诊断；合法时统一编号
    if conflicts:
        return [], conflicts

    out: list[dict] = []
    for i, m in enumerate(merged, start=1):
        out.append({
            "id": f"M{i}",
            "start": m["start"],
            "end": m["end"],
            "text": m["text"],
            "sources": m["sources"],
        })
    return out, []


def evaluate_combinations(segments: Sequence[Segment],
                          legal_per_seam: Sequence[Sequence[int]]
                          ) -> list[Combination]:
    """枚举各接缝合法候选的笛卡尔积，逐个做标签相交校验。"""
    lengths = [len(s.text) for s in segments]
    texts = [s.text for s in segments]
    results: list[Combination] = []
    for overlaps in product(*legal_per_seam):
        offsets = segment_offsets(lengths, overlaps)
        merged = build_merged_text(texts, overlaps)
        _, conflicts = project_and_merge_labels(segments, offsets)
        results.append(Combination(
            overlaps=list(overlaps),
            merged_length=len(merged),
            legal=not conflicts,
            conflicts=conflicts,
        ))
    return results


def normalize_request(raw_segments, raw_seams):
    """校验并规范化请求体（不含数据库状态检查）。

    入参为请求里的原始对象；返回 (session_refs, candidates_per_seam)，
    session_refs: [{session_id, revision}]，candidates_per_seam: [[int,...], ...]。
    """
    if not isinstance(raw_segments, list):
        raise StitchValidationError("segments 必须是数组")
    n = len(raw_segments)
    if not (MIN_SEGMENTS <= n <= MAX_SEGMENTS):
        raise StitchValidationError(
            f"段数必须在 {MIN_SEGMENTS}~{MAX_SEGMENTS} 之间，当前 {n}")
    refs: list[dict] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw_segments, start=1):
        if not isinstance(item, dict):
            raise StitchValidationError(f"第 {i} 段必须是对象")
        sid = item.get("session_id")
        rev = item.get("revision")
        if not isinstance(sid, str) or not sid.strip():
            raise StitchValidationError(f"第 {i} 段缺少有效的 session_id")
        sid = sid.strip()
        if sid in seen_ids:
            raise StitchValidationError(f"会话 {sid!r} 在第 {i} 段重复，各段必须来自不同会话")
        seen_ids.add(sid)
        if not isinstance(rev, int) or isinstance(rev, bool) or rev <= 0:
            raise StitchValidationError(f"第 {i} 段的 revision 必须是正整数")
        refs.append({"session_id": sid, "revision": rev})

    if not isinstance(raw_seams, list):
        raise StitchValidationError("seams 必须是数组")
    if len(raw_seams) != n - 1:
        raise StitchValidationError(
            f"{n} 段必须有 {n - 1} 道接缝，当前提供 {len(raw_seams)} 道")
    candidates_per_seam: list[list[int]] = []
    for i, item in enumerate(raw_seams, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("candidates"), list):
            raise StitchValidationError(f"第 {i} 道接缝必须是含 candidates 数组的对象")
        cands = item["candidates"]
        if not (MIN_CANDIDATES <= len(cands) <= MAX_CANDIDATES):
            raise StitchValidationError(
                f"第 {i} 道接缝的候选数必须在 {MIN_CANDIDATES}~{MAX_CANDIDATES} 个，"
                f"当前 {len(cands)} 个")
        norm: list[int] = []
        local_seen: set[int] = set()
        for k in cands:
            if not isinstance(k, int) or isinstance(k, bool):
                raise StitchValidationError(f"第 {i} 道接缝的候选重叠码点数必须是整数")
            if k in local_seen:
                raise StitchValidationError(f"第 {i} 道接缝的候选重叠长度 {k} 重复")
            local_seen.add(k)
            norm.append(k)
        candidates_per_seam.append(norm)
    return refs, candidates_per_seam


def plan_stitch(segments: Sequence[Segment],
                candidates_per_seam: Sequence[Sequence[int]]) -> dict:
    """拼稿主流程（输入均已通过会话状态校验）。

    返回 dict，status 取值：
    - "unique"      唯一最短合法拼法，含 result（可下载的完整 JSON 内容）；
    - "tie"         最短长度并列，含 ties（每组的各接缝重叠长度）供复核；
    - "no_solution" 无合法拼法，含 seam_rejections / label_conflicts 等诊断。
    """
    n = len(segments)
    # 1) 逐接缝筛候选
    legal_per_seam: list[list[int]] = []
    seam_rejections: list[dict] = []
    for i in range(n - 1):
        legal, rejected = evaluate_seam(
            segments[i].text, segments[i + 1].text,
            candidates_per_seam[i])
        for r in rejected:
            seam_rejections.append({
                "seam_index": i + 1,
                "candidate": r.candidate,
                "reason": r.reason,
            })
        legal_per_seam.append(legal)

    empty_seams = [i + 1 for i, legal in enumerate(legal_per_seam) if not legal]
    if empty_seams:
        return {
            "status": "no_solution",
            "reason": "seam_no_legal_candidate",
            "message": f"第 {'、'.join(map(str, empty_seams))} 道接缝没有合法候选，"
                       "请检查接缝重叠长度或文本",
            "seam_rejections": seam_rejections,
            "empty_seams": empty_seams,
        }

    # 2) 枚举组合并做标签相交校验
    combos = evaluate_combinations(segments, legal_per_seam)
    legal_combos = [c for c in combos if c.legal]
    if not legal_combos:
        # 全部组合都因标签部分相交而作废：汇总冲突来源
        conflict_dump: list[dict] = []
        seen_pairs: set[tuple] = set()
        for c in combos:
            for cf in c.conflicts:
                key = (cf.a["session_index"], cf.a["label_id"],
                       cf.b["session_index"], cf.b["label_id"])
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                conflict_dump.append({
                    "overlaps": c.overlaps,
                    "label_a": cf.a,
                    "label_b": cf.b,
                    "overlap_interval": list(cf.interval),
                })
        return {
            "status": "no_solution",
            "reason": "label_conflict",
            "message": "所有候选拼法都存在非完全重合的标签相交，请检查标签或接缝",
            "seam_rejections": seam_rejections,
            "label_conflicts": conflict_dump,
        }

    # 3) 取最短
    best_len = min(c.merged_length for c in legal_combos)
    best = [c for c in legal_combos if c.merged_length == best_len]
    best.sort(key=lambda c: c.overlaps)
    if len(best) > 1:
        return {
            "status": "tie",
            "message": f"最短合稿 {best_len} 码点存在 {len(best)} 种并列拼法，"
                       "请复核后收窄接缝候选",
            "merged_length": best_len,
            "ties": [{"overlaps": c.overlaps} for c in best],
            "seam_rejections": seam_rejections,
        }

    # 4) 唯一最短：产出完整结果
    overlaps = best[0].overlaps
    lengths = [len(s.text) for s in segments]
    offsets = segment_offsets(lengths, overlaps)
    texts = [s.text for s in segments]
    merged_text = build_merged_text(texts, overlaps)
    labels, _ = project_and_merge_labels(segments, offsets)
    result = {
        "merged": merged_text,
        "segments": [{
            "session_index": s.session_index,
            "session_id": s.session_id,
            "revision": s.revision,
            "start": offsets[i],
            "end": offsets[i] + lengths[i],
            "length": lengths[i],
        } for i, s in enumerate(segments)],
        "labels": labels,
        "meta": {
            "overlaps": overlaps,
            "merged_length": best_len,
            "segment_count": n,
        },
    }
    return {
        "status": "unique",
        "message": f"唯一最短合法拼法：合稿 {best_len} 码点",
        "merged_length": best_len,
        "overlaps": overlaps,
        "seam_rejections": seam_rejections,
        "result": result,
    }
