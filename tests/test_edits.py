"""剪辑字幕导出测试。

覆盖：
- 基于快照冻结生效文本的导出（快照后的新更正 / 新定稿不影响结果）；
- 每条字幕与每个 clip 取交集，非空才生成 cue（端点相触为空交集）；
- clips 按输入顺序首尾拼接：重排、重复（保留副本）、重叠均正确累计偏移；
- 跨片段字幕拆分、时间上重叠的字幕不合并；
- cue 按 (start_ms, clip_index, seq) 升序；
- timings 须恰好覆盖全部字幕 seq：缺字幕 / 未知 seq（含缺口序号）/
  重复 seq -> 422；时间为负、非整数、起点不小于终点 -> 422；
- format=vtt 输出合法 WebVTT（时间戳格式、正文转义、换行折叠）；
- 快照不存在 -> 404；导出只读，不改快照或当前定稿。
"""


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def correct(client, channel, seq, expected_version, text, reason="r"):
    resp = client.post(
        f"/channels/{channel}/finalized/{seq}/corrections",
        json={
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def make_snapshot(client, channel, key):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200
    return resp.json()


def edit(client, snapshot_id, timings, clips, **params):
    payload = {"timings": timings, "clips": clips}
    return client.post(f"/snapshots/{snapshot_id}/edits", json=payload, params=params)


def timing(seq, start_ms, end_ms):
    return {"seq": seq, "start_ms": start_ms, "end_ms": end_ms}


def clip(start_ms, end_ms):
    return {"start_ms": start_ms, "end_ms": end_ms}


def cue_tuples(body):
    return [
        (c["seq"], c["clip_index"], c["start_ms"], c["end_ms"], c["text"])
        for c in body["cues"]
    ]


# ---------- 基本映射语义 ----------


def test_edit_basic_intersection_and_cumulative_offsets(client):
    for seq in (1, 2, 3):
        submit(client, "c1", seq, 1, f"第{seq}条")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # clips 重排：先取原始 [4000,6000)，再取 [0,3000)，成片时长 2000+3000
    resp = edit(
        client,
        sid,
        [timing(1, 0, 2000), timing(2, 2000, 4000), timing(3, 4000, 6000)],
        [clip(4000, 6000), clip(0, 3000)],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_id"] == sid
    assert body["channel"] == "c1"
    assert body["duration_ms"] == 5000
    assert cue_tuples(body) == [
        (3, 0, 0, 2000, "第3条"),      # 完整落入 clip0，平移到成片起点
        (1, 1, 2000, 4000, "第1条"),   # clip1 偏移 2000
        (2, 1, 4000, 5000, "第2条"),   # 与 clip1 部分交集 [2000,3000)，平移 2000
    ]


def test_edit_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始文本")
    correct(client, "c1", 1, 0, "快照时生效")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 快照后再更正、再定稿，均不影响导出
    correct(client, "c1", 1, 1, "快照后再改")
    submit(client, "c1", 2, 1, "新字幕")

    body = edit(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)]).json()
    assert cue_tuples(body) == [(1, 0, 0, 1000, "快照时生效")]


def test_edit_subtitle_spanning_clips_is_split(client):
    submit(client, "c1", 1, 1, "跨片段")
    snap = make_snapshot(client, "c1", "k1")

    body = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 0, 5000)],
        [clip(0, 2000), clip(2000, 4000), clip(4000, 6000)],
    ).json()
    assert body["duration_ms"] == 6000
    assert cue_tuples(body) == [
        (1, 0, 0, 2000, "跨片段"),
        (1, 1, 2000, 4000, "跨片段"),
        (1, 2, 4000, 5000, "跨片段"),  # 片尾截断到字幕终点
    ]


def test_edit_duplicate_clips_keep_copies(client):
    submit(client, "c1", 1, 1, "重复片段")
    snap = make_snapshot(client, "c1", "k1")

    body = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 1000, 2000)],
        [clip(0, 3000), clip(0, 3000)],
    ).json()
    # 同一片段出现两次：各自生成副本，第二段按累计时长偏移 3000
    assert cue_tuples(body) == [
        (1, 0, 1000, 2000, "重复片段"),
        (1, 1, 4000, 5000, "重复片段"),
    ]


def test_edit_overlapping_clips_and_unmerged_subtitles(client):
    submit(client, "c1", 1, 1, "甲")
    submit(client, "c1", 2, 1, "乙")
    snap = make_snapshot(client, "c1", "k1")

    # 两个 clip 互相重叠；两条字幕时间也互相重叠：各自独立生成 cue，不合并
    body = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 0, 3000), timing(2, 1000, 2000)],
        [clip(0, 4000), clip(2000, 5000)],
    ).json()
    assert cue_tuples(body) == [
        (1, 0, 0, 3000, "甲"),
        (2, 0, 1000, 2000, "乙"),
        (1, 1, 4000, 5000, "甲"),  # clip1 偏移 4000，交集 [2000,3000)
    ]


def test_edit_empty_intersection_generates_no_cue(client):
    submit(client, "c1", 1, 1, "相触")
    submit(client, "c1", 2, 1, "相离")
    snap = make_snapshot(client, "c1", "k1")

    # 左闭右开：字幕终点与片段起点相触（1000）为空交集；完全相离更无 cue
    body = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 0, 1000), timing(2, 3000, 4000)],
        [clip(1000, 2000), clip(5000, 6000)],
    ).json()
    assert body["cues"] == []
    assert body["duration_ms"] == 2000


def test_edit_cues_sorted_by_start_clip_index_seq(client):
    submit(client, "c1", 1, 1, "一")
    submit(client, "c1", 2, 1, "二")
    snap = make_snapshot(client, "c1", "k1")

    # 两条字幕同起点、两个重复片段：排序键依次为 start_ms、clip_index、seq
    body = edit(
        client,
        snap["snapshot_id"],
        [timing(2, 0, 1000), timing(1, 0, 1000)],  # 输入顺序不影响输出排序
        [clip(0, 1000), clip(0, 1000)],
    ).json()
    assert cue_tuples(body) == [
        (1, 0, 0, 1000, "一"),
        (2, 0, 0, 1000, "二"),
        (1, 1, 1000, 2000, "一"),
        (2, 1, 1000, 2000, "二"),
    ]


def test_edit_gap_seqs_need_no_timing(client):
    submit(client, "c1", 1, 1, "第一条")
    submit(client, "c1", 3, 1, "第三条")
    decide_gap(client, "c1", 2, 2)
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 缺口序号 2 不需要 timing：只覆盖字幕 seq 即可
    body = edit(
        client, sid, [timing(1, 0, 1000), timing(3, 1000, 2000)], [clip(0, 2000)]
    ).json()
    assert cue_tuples(body) == [
        (1, 0, 0, 1000, "第一条"),
        (3, 0, 1000, 2000, "第三条"),
    ]

    # 为缺口序号提供 timing -> 422 unknown_timing_seq
    resp = edit(
        client,
        sid,
        [timing(1, 0, 1000), timing(2, 0, 500), timing(3, 1000, 2000)],
        [clip(0, 2000)],
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "unknown_timing_seq"


def test_edit_empty_snapshot_and_empty_clips(client):
    snap = make_snapshot(client, "ghost", "k1")
    sid = snap["snapshot_id"]

    # 空快照：timings 为空即恰好覆盖，任意 clips 都得到空 cue 列表
    body = edit(client, sid, [], [clip(0, 1000), clip(1000, 2500)]).json()
    assert body["duration_ms"] == 2500
    assert body["cues"] == []

    # 空 timings 之外：空 clips 对非空快照合法，成片时长 0、无 cue
    submit(client, "c1", 1, 1, "唯一")
    snap2 = make_snapshot(client, "c1", "k1")
    body = edit(client, snap2["snapshot_id"], [timing(1, 0, 1000)], []).json()
    assert body["duration_ms"] == 0
    assert body["cues"] == []

    # 空快照但 timings 非空 -> 未知 seq
    resp = edit(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "unknown_timing_seq"


# ---------- timings 覆盖校验 ----------


def test_edit_timings_must_exactly_cover_subtitle_seqs(client):
    submit(client, "c1", 1, 1, "一")
    submit(client, "c1", 2, 1, "二")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 缺字幕 seq -> 422 timings_missing_seqs
    resp = edit(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "timings_missing_seqs"

    # 空 timings 对非空快照同样是缺漏
    resp = edit(client, sid, [], [clip(0, 1000)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "timings_missing_seqs"

    # 未知 seq（超出水位线）-> 422 unknown_timing_seq
    resp = edit(
        client, sid, [timing(1, 0, 1000), timing(2, 0, 1000), timing(9, 0, 1000)],
        [clip(0, 1000)],
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "unknown_timing_seq"

    # 重复 seq -> 422 duplicate_timing_seq
    resp = edit(
        client, sid, [timing(1, 0, 1000), timing(1, 0, 500), timing(2, 0, 1000)],
        [clip(0, 1000)],
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "duplicate_timing_seq"

    # 恰好覆盖（顺序任意）-> 200
    resp = edit(client, sid, [timing(2, 0, 1000), timing(1, 0, 1000)], [clip(0, 1000)])
    assert resp.status_code == 200


def test_edit_time_values_validation(client):
    submit(client, "c1", 1, 1, "一")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    # 负时间
    assert edit(client, sid, [timing(1, -1, 1000)], [clip(0, 1000)]).status_code == 422
    assert edit(client, sid, [timing(1, 0, 1000)], [clip(-1, 1000)]).status_code == 422
    # 起点不小于终点（含相等）
    assert edit(client, sid, [timing(1, 1000, 1000)], [clip(0, 1000)]).status_code == 422
    assert edit(client, sid, [timing(1, 2000, 1000)], [clip(0, 1000)]).status_code == 422
    assert edit(client, sid, [timing(1, 0, 1000)], [clip(1000, 1000)]).status_code == 422
    assert edit(client, sid, [timing(1, 0, 1000)], [clip(2000, 1000)]).status_code == 422
    # 非整数时间
    assert edit(client, sid, [timing(1, 0, 1000.5)], [clip(0, 1000)]).status_code == 422
    assert edit(client, sid, [timing(1, 0, 1000)], [clip(0.5, 1000)]).status_code == 422
    # seq 非法（0、负数、非整数）
    assert edit(client, sid, [timing(0, 0, 1000)], [clip(0, 1000)]).status_code == 422
    assert edit(client, sid, [timing(-1, 0, 1000)], [clip(0, 1000)]).status_code == 422
    # 缺字段 / 结构错误
    assert client.post(
        f"/snapshots/{sid}/edits", json={"clips": [clip(0, 1000)]}
    ).status_code == 422
    assert client.post(
        f"/snapshots/{sid}/edits", json={"timings": [timing(1, 0, 1000)]}
    ).status_code == 422
    assert client.post(
        f"/snapshots/{sid}/edits",
        json={"timings": [{"seq": 1, "start_ms": 0}], "clips": []},
    ).status_code == 422

    # 边界合法：起点 0、时长 1 毫秒
    assert edit(client, sid, [timing(1, 0, 1)], [clip(0, 1)]).status_code == 200


def test_edit_unknown_snapshot_returns_404(client):
    resp = edit(client, "does-not-exist", [timing(1, 0, 1000)], [clip(0, 1000)])
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


def test_edit_invalid_format_returns_422(client):
    snap = make_snapshot(client, "ghost", "k1")
    resp = edit(client, snap["snapshot_id"], [], [clip(0, 1)], format="srt")
    assert resp.status_code == 422


# ---------- WebVTT 输出 ----------


def test_edit_vtt_output(client):
    submit(client, "c1", 1, 1, "第一条")
    submit(client, "c1", 2, 1, "第二条")
    snap = make_snapshot(client, "c1", "k1")

    resp = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 0, 1500), timing(2, 1500, 3000)],
        [clip(0, 3000)],
        format="vtt",
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/vtt")
    assert resp.text == (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:01.500\n"
        "第一条\n"
        "\n"
        "00:00:01.500 --> 00:00:03.000\n"
        "第二条\n"
    )


def test_edit_vtt_timestamp_hours_and_split_cues(client):
    submit(client, "c1", 1, 1, "长片")
    snap = make_snapshot(client, "c1", "k1")

    # 字幕横跨 1 小时边界并被两个 clip 拆分：校验 HH:MM:SS.mmm 与累计偏移
    resp = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 3_599_000, 3_601_500)],
        [clip(3_599_000, 3_600_000), clip(3_600_000, 3_602_000)],
        format="vtt",
    )
    assert resp.text == (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:01.000\n"
        "长片\n"
        "\n"
        "00:00:01.000 --> 00:00:02.500\n"
        "长片\n"
    )

    # 直接落在 1 小时之后的 cue：小时位不取模
    resp = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 3_599_000, 3_601_500)],
        [clip(0, 3_700_000)],
        format="vtt",
    )
    assert "00:59:59.000 --> 01:00:01.500\n长片\n" in resp.text


def test_edit_vtt_escapes_body_text(client):
    submit(client, "c1", 1, 1, "A & B <i> --> 箭头\n第二行")
    snap = make_snapshot(client, "c1", "k1")

    resp = edit(
        client,
        snap["snapshot_id"],
        [timing(1, 0, 1000)],
        [clip(0, 1000)],
        format="vtt",
    )
    assert resp.status_code == 200
    # & < > 转义为实体（--> 随之失效），换行折叠为空格，不出现空白行截断
    assert "A &amp; B &lt;i&gt; --&gt; 箭头 第二行\n" in resp.text
    assert "\n\n\n" not in resp.text

    # JSON 视图不转义，返回原文
    body = edit(
        client, snap["snapshot_id"], [timing(1, 0, 1000)], [clip(0, 1000)]
    ).json()
    assert body["cues"][0]["text"] == "A & B <i> --> 箭头\n第二行"


def test_edit_vtt_empty_cues(client):
    snap = make_snapshot(client, "ghost", "k1")
    resp = edit(client, snap["snapshot_id"], [], [clip(0, 1000)], format="vtt")
    assert resp.status_code == 200
    assert resp.text == "WEBVTT\n"


# ---------- 只读：不修改快照或当前状态 ----------


def test_edit_does_not_modify_snapshot_or_live_state(client):
    submit(client, "c1", 1, 1, "原始")
    correct(client, "c1", 1, 0, "生效")
    snap = make_snapshot(client, "c1", "k1")
    sid = snap["snapshot_id"]

    before_original = client.get(f"/snapshots/{sid}/items").json()
    before_effective = client.get(
        f"/snapshots/{sid}/items", params={"view": "effective"}
    ).json()

    body = edit(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)]).json()
    assert body["cues"][0]["text"] == "生效"

    # 快照两个视图保持原样
    assert client.get(f"/snapshots/{sid}/items").json() == before_original
    assert (
        client.get(f"/snapshots/{sid}/items", params={"view": "effective"}).json()
        == before_effective
    )
    # 当前定稿与更正链不受影响
    live = client.get("/channels/c1/finalized", params={"view": "effective"}).json()
    assert live["items"][0]["text"] == "生效"
    assert live["items"][0]["correction_version"] == 1

    # 重复导出结果稳定（幂等的纯计算）
    again = edit(client, sid, [timing(1, 0, 1000)], [clip(0, 1000)]).json()
    assert again == body
