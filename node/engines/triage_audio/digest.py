"""确定性模板：把 VAD / 打标 / ASR 的原始输出组装成 digest（协议 digest schema）。纯函数，不碰模型，单测直接打。

规则出处：架构.md §8、T4 观察①–④。
- segments 覆盖全时长、左闭右开、按 start 升序；语音 / 非语音交替。
- labels 只留 ≥ 阈值的；没有 → labels=[]、label_status=unclassified，并在 gaps 里说"无法判定类别"。
- asr：非语音段 null（不适用）；语音段失败则省略字段（取不到）；取不到的 confidence 用 "n/a"。
- gaps 由规则稳定生成，不依赖打标质量。
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_GAP_SEC = 0.05     # 语音段之间小于这个的空隙不单独成段
SHORT_GAP_SEC = 0.6    # 夹在两段语音之间、短于此的非语音段：可能是句内停顿，也可能是有内容的短事件（一声口哨）——点出来，不替上层判断


@dataclass
class RawSegment:
    start: float
    end: float
    speech: bool
    labels: list[dict] = field(default_factory=list)        # 全部候选 [{tag, score}]，未过阈值
    tag_failed: bool = False
    asr: dict | None = None                                  # {text, lang, confidence?}；语音段失败为 None
    asr_failed: bool = False
    vad_missed: bool = False                                 # VAD 判非语音、但打标 Speech ≥ 阈值 → 补跑了 ASR


def timeline(duration: float, speech: list[tuple[float, float]]) -> list[RawSegment]:
    """把语音区间补成覆盖 [0, duration) 的交替时间轴。太短的空隙并入后面的语音段。"""
    segs: list[RawSegment] = []
    cur = 0.0
    for st, en in speech:
        if st - cur >= MIN_GAP_SEC:
            segs.append(RawSegment(round(cur, 3), round(st, 3), False))
            seg_start = st
        else:
            seg_start = cur
        segs.append(RawSegment(round(seg_start, 3), round(en, 3), True))
        cur = en
    if duration - cur >= MIN_GAP_SEC:
        segs.append(RawSegment(round(cur, 3), round(duration, 3), False))
    elif segs:
        segs[-1].end = round(duration, 3)
    return segs


def _fmt(t: float) -> str:
    return f"{t:.2f}"


def build_digest(media_id: str, duration: float, segs: list[RawSegment], tools: dict[str, str],
                 threshold: float, capabilities_available: list[str], timings_ms: dict[str, int]) -> dict:
    out_segs = []
    gaps: list[str] = []
    speech_dur = 0.0
    for s in segs:
        span = f"{_fmt(s.start)}–{_fmt(s.end)}s"
        kept = sorted((l for l in s.labels if l["score"] >= threshold), key=lambda l: -l["score"])
        top = max((l["score"] for l in s.labels), default=None)
        if s.tag_failed:
            status = "failed"
        elif kept:
            status = "ok"
        else:
            status = "unclassified"
        seg = {"start": s.start, "end": s.end,
               "labels": [{"tag": l["tag"], "score": round(float(l["score"]), 3)} for l in kept] if status == "ok" else [],
               "label_status": status}
        if s.speech:
            speech_dur += s.end - s.start
            if s.asr is not None:
                asr = {"text": s.asr.get("text", ""), "lang": s.asr.get("lang") or "n/a"}
                asr["confidence"] = s.asr["confidence"] if isinstance(s.asr.get("confidence"), (int, float)) else "n/a"
                seg["asr"] = asr
                dur_ms = int((s.end - s.start) * 1000)
                top_txt = f"{s.labels[0]['tag']} {s.labels[0]['score']:.2f}" if s.labels else "无明确类别"
                if s.vad_missed:
                    gaps.append(f"{span} VAD 未判为语音，但打标 Speech {asr['confidence']:.2f} ≥ 阈值 → 已补跑 ASR：「{asr['text']}」")
                if not asr["text"].strip():
                    gaps.append(f"{span} 为语音段，但 ASR 无输出 → 内容未知")
                elif isinstance(asr["confidence"], (int, float)) and asr["confidence"] < threshold:
                    # 只陈述事实，不猜原因：可能是喷麦，也可能是哼唱 / 唱的一个音 / 太短的一句话
                    note = f"{span} 被 VAD 判为语音，但打标 Speech 仅 {asr['confidence']:.2f}（top {top_txt}）→ ASR 文本「{asr['text']}」置信度低"
                    if dur_ms < 1000:
                        note += f"；段长 {dur_ms}ms，打标在这么短的段上本身也不可靠"
                    gaps.append(note)
            else:
                gaps.append(f"{span} 为语音段，但 ASR 失败 → 内容未知")
        else:
            seg["asr"] = None
            if status == "failed":
                gaps.append(f"{span} 为非语音段，打标失败 → 无法判定类别")
            elif status == "unclassified":
                shown = f"top {top:.3f} < 阈值 {threshold:.2f}" if top is not None else f"无标签，阈值 {threshold:.2f}"
                gaps.append(f"{span} 为非语音段，但分类置信度过低（{shown}）→ 无法判定类别")
            else:
                cats = "、".join(l["tag"] for l in kept[:2])
                gaps.append(f"{span} 为非语音段（{cats}），只知类别、不知内容（无音高 / 谱面 / 歌词）")
        out_segs.append(seg)

    # 夹在两段语音中间的短非语音段：不合并（合并会抹掉内容），但明确指出它的位置与标签
    for i in range(1, len(segs) - 1):
        s = segs[i]
        if not s.speech and segs[i - 1].speech and segs[i + 1].speech and (s.end - s.start) < SHORT_GAP_SEC:
            kept = [l for l in s.labels if l["score"] >= threshold]
            what = "、".join(l["tag"] for l in kept[:2]) if kept else "类别无法判定"
            gaps.append(f"结构事实：{_fmt(s.start)}–{_fmt(s.end)}s（{int((s.end - s.start) * 1000)}ms，{what}）夹在两段语音之间——可能只是停顿，也可能是有内容的短事件，前后两段语音可能是同一句话")

    # 结构事实（T4 观察①）：非语音在前、语音在后 → 指令可能指向前面那段。任务无关，不猜意图。
    first_speech = next((i for i, s in enumerate(segs) if s.speech), None)
    if first_speech is not None and first_speech > 0:
        gaps.append("结构事实：非语音段位于时间轴前部、语音段在后（指令在后）")
    for name, st in tools.items():
        if st in ("failed", "partial"):
            gaps.append(f"工具 {name} 状态 {st}，相关字段不完整")

    return {
        "media_id": media_id,
        "kind": "audio",
        "duration_sec": round(duration, 3),
        "timeline_unit": "sec",
        "segments": out_segs,
        "global": {"speech_ratio": round(speech_dur / duration, 3) if duration > 0 else 0.0},
        "tools": tools,
        "gaps": gaps,
        "capabilities_available": list(capabilities_available),
        "timings_ms": timings_ms,
    }
