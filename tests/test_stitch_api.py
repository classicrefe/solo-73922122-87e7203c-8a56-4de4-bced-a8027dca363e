"""拼稿端到端 API 测试：快照一致性、状态闸门、三种裁决、下载规则。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_session(client, original, revised, labels=None, confirm=True):
    r = client.post("/api/sessions", json={
        "original": original, "revised": revised,
        "labels": labels or []})
    assert r.status_code == 201, r.text
    st = r.json()
    if confirm:
        r = client.post(f"/api/sessions/{st['session_id']}/confirm",
                        json={"revision": st["revision"]})
        assert r.status_code == 200, r.text
    return st


def stitch(client, refs, seams):
    return client.post("/api/stitches", json={"segments": refs, "seams": seams})


@pytest.fixture()
def three_segments(client):
    """三段可拼示例：
    s1 甲乙丙丁戊 / s2 丁戊己庚 / s3 己庚辛壬；接缝合法重叠均为 2。"""
    s1 = make_session(client, "甲乙丙丁戊", "甲乙丙丁戊",
                      [{"id": "L1", "start": 3, "end": 5}])
    s2 = make_session(client, "丁戊己庚", "丁戊己庚",
                      [{"id": "L1", "start": 0, "end": 2},
                       {"id": "L2", "start": 2, "end": 4}])
    s3 = make_session(client, "己庚辛壬", "己庚辛壬",
                      [{"id": "L9", "start": 0, "end": 2}])
    return s1, s2, s3


class TestStitchHappyPath:
    def test_unique_shortest_creates_download(self, client, three_segments):
        s1, s2, s3 = three_segments
        refs = [{"session_id": s["session_id"], "revision": s["revision"]}
                for s in (s1, s2, s3)]
        r = stitch(client, refs, [{"candidates": [1, 2, 3]},
                                  {"candidates": [1, 2]}])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "unique"
        assert body["overlaps"] == [2, 2]
        assert body["result"]["merged"] == "甲乙丙丁戊己庚辛壬"
        # 完全重合的标签合并，来源齐全
        m1, m2 = body["result"]["labels"]
        assert [x["label_id"] for x in m1["sources"]] == ["L1", "L1"]
        assert [x["session_index"] for x in m2["sources"]] == [2, 3]
        # 各段在合稿中的半开区间
        assert [seg["start"] for seg in body["result"]["segments"]] == [0, 3, 5]
        assert body["download_url"]

        d = client.get(body["download_url"])
        assert d.status_code == 200
        assert "attachment" in d.headers["content-disposition"]
        data = d.json()
        assert data["merged"] == "甲乙丙丁戊己庚辛壬"
        assert data["meta"]["overlaps"] == [2, 2]

    def test_rejected_candidates_explained(self, client, three_segments):
        s1, s2, s3 = three_segments
        refs = [{"session_id": s["session_id"], "revision": s["revision"]}
                for s in (s1, s2, s3)]
        body = stitch(client, refs, [{"candidates": [1, 2, 3]},
                                     {"candidates": [1, 2]}]).json()
        rejected = {(x["seam_index"], x["candidate"])
                    for x in body["seam_rejections"]}
        assert rejected == {(1, 1), (1, 3), (2, 1)}


class TestStitchGates:
    def test_session_not_found(self, client, three_segments):
        s1, s2, _ = three_segments
        refs = [{"session_id": s1["session_id"], "revision": 1},
                {"session_id": "deadbeefdeadbeef", "revision": 1}]
        r = stitch(client, refs, [{"candidates": [2]}])
        assert r.status_code == 404
        d = r.json()["detail"]
        assert d["code"] == "session_not_found" and d["session_index"] == 2

    def test_stale_revision_stops_and_points_to_segment(self, client, three_segments):
        s1, s2, s3 = three_segments
        # 另起窗口修改 s2 的输入 -> revision 前进、结果清除
        u = client.put(f"/api/sessions/{s2['session_id']}/inputs", json={
            "original": "丁戊己庚", "revised": "丁戊己庚X", "labels": []})
        assert u.status_code == 200
        refs = [{"session_id": s["session_id"], "revision": s["revision"]}
                for s in (s1, s2, s3)]
        r = stitch(client, refs, [{"candidates": [2]}, {"candidates": [2]}])
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["code"] == "stale_revision"
        assert d["session_index"] == 2
        assert d["current_revision"] == 2

    def test_unconfirmed_session_rejected(self, client, three_segments):
        s1, s2, s3 = three_segments
        raw = make_session(client, "辛壬癸", "辛壬癸", confirm=False)
        refs = [{"session_id": s1["session_id"], "revision": 1},
                {"session_id": s2["session_id"], "revision": 1},
                {"session_id": raw["session_id"], "revision": 1}]
        r = stitch(client, refs, [{"candidates": [2]}, {"candidates": [2]}])
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["code"] == "unconfirmed" and d["session_index"] == 3
        assert client.get("/api/stitches/nonexistent/download").status_code == 404

    def test_duplicate_session_rejected(self, client, three_segments):
        s1, s2, _ = three_segments
        refs = [{"session_id": s1["session_id"], "revision": 1},
                {"session_id": s1["session_id"], "revision": 1},
                {"session_id": s2["session_id"], "revision": 1}]
        r = stitch(client, refs, [{"candidates": [2]}, {"candidates": [2]}])
        assert r.status_code == 400 and "重复" in r.json()["detail"]

    def test_segment_count_bounds(self, client, three_segments):
        s1 = three_segments[0]
        r = stitch(client, [{"session_id": s1["session_id"], "revision": 1}], [])
        assert r.status_code == 400

    def test_candidate_bounds(self, client, three_segments):
        s1, s2, _ = three_segments
        refs = [{"session_id": s["session_id"], "revision": s["revision"]}
                for s in (s1, s2)]
        r = stitch(client, refs, [{"candidates": []}])
        assert r.status_code == 400
        r = stitch(client, refs, [{"candidates": [1, 2, 3, 4]}])
        assert r.status_code == 400


class TestStitchOutcomes:
    def test_tie_no_file(self, client):
        # 合稿长度 = Σ段长 - Σ重叠；并列 ⟺ 重叠总和相同而向量不同。
        # 三段均长 4、两接缝候选各 {2,3}；s1 的 [2,3) 与 s3 的 [0,2)
        # 只让重叠和最大的 (3,3) 部分相交失效，(2,3) 与 (3,2) 并列最短。
        a = make_session(client, "aaaa", "aaaa",
                         [{"id": "x", "start": 2, "end": 3}])
        b = make_session(client, "aaaa", "aaaa")
        c = make_session(client, "aaaa", "aaaa",
                         [{"id": "y", "start": 0, "end": 2}])
        refs = [{"session_id": s["session_id"], "revision": 1} for s in (a, b, c)]
        r = stitch(client, refs, [{"candidates": [2, 3]}, {"candidates": [2, 3]}])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "tie"
        assert [t["overlaps"] for t in body["ties"]] == [[2, 3], [3, 2]]
        assert body["download_url"] is None and body["stitch_id"] is None
        # 并列不落库
        assert client.get("/api/stitches/anything/download").status_code == 404

    def test_no_solution_no_file(self, client, three_segments):
        s1, s2, s3 = three_segments
        refs = [{"session_id": s["session_id"], "revision": s["revision"]}
                for s in (s1, s2, s3)]
        # 接缝1 给的候选全都不匹配
        r = stitch(client, refs, [{"candidates": [1, 3]}, {"candidates": [2]}])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "no_solution"
        assert body["reason"] == "seam_no_legal_candidate"
        assert body["empty_seams"] == [1]
        assert body["download_url"] is None

    def test_label_conflict_no_file(self, client):
        # 两段在重叠区的标签只是部分相交 -> 所有拼法无效
        s1 = make_session(client, "甲乙丙", "甲乙丙",
                          [{"id": "x", "start": 1, "end": 3}])
        s2 = make_session(client, "乙丙丁", "乙丙丁",
                          [{"id": "y", "start": 1, "end": 3}])
        refs = [{"session_id": s1["session_id"], "revision": 1},
                {"session_id": s2["session_id"], "revision": 1}]
        r = stitch(client, refs, [{"candidates": [2]}])
        body = r.json()
        assert body["status"] == "no_solution"
        assert body["reason"] == "label_conflict"
        ids = {body["label_conflicts"][0]["label_a"]["label_id"],
               body["label_conflicts"][0]["label_b"]["label_id"]}
        assert ids == {"x", "y"}

    def test_codepoint_aware_overlap(self, client):
        # emoji 单码点：重叠 1 合法
        s1 = make_session(client, "a😀", "a😀")
        s2 = make_session(client, "😀b", "😀b")
        refs = [{"session_id": s1["session_id"], "revision": 1},
                {"session_id": s2["session_id"], "revision": 1}]
        body = stitch(client, refs, [{"candidates": [1]}]).json()
        assert body["status"] == "unique"
        assert body["result"]["merged"] == "a😀b"

    def test_same_snapshot_reads_all_segments(self, client, three_segments):
        """拼稿进行中另一窗口修改输入：本次请求要么在修改前读完（成功），
        要么看到新 revision 整次 409，绝不会出现半新半旧的合稿。"""
        s1, s2, s3 = three_segments
        refs = [{"session_id": s["session_id"], "revision": s["revision"]}
                for s in (s1, s2, s3)]
        # 修改 s3 后，带旧 revision 的拼稿必须整体失败
        client.put(f"/api/sessions/{s3['session_id']}/inputs", json={
            "original": "己庚辛壬", "revised": "己庚辛壬Z", "labels": []})
        r = stitch(client, refs, [{"candidates": [2]}, {"candidates": [2]}])
        assert r.status_code == 409
        assert r.json()["detail"]["session_index"] == 3
