from __future__ import annotations

import asyncio
import base64
import functools
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

from engines.common.media_tools import MediaError, finite_options, limit

MAX_DURATION = limit("MMP_AUDIO_MAX_DURATION_SEC", 3600)
MAX_BYTES = limit("MMP_AUDIO_MAX_OUTPUT_BYTES", 64 * 1024 * 1024)
MAX_LOG_BYTES = 2 * 1024 * 1024
DEMUXERS = "wav,mp3,flac,ogg,mov,aac,aiff,asf,amr,au,matroska,webm"
FORMATS = {
    "wav": ("wav", "pcm_s16le", "audio/wav", "wav"),
    "flac": ("flac", "flac", "audio/flac", "flac"),
    "mp3": ("mp3", "libmp3lame", "audio/mpeg", "mp3"),
    "ogg": ("ogg", "libvorbis", "audio/ogg", "ogg"),
    "opus": ("opus", "libopus", "audio/ogg", "opus"),
    "m4a": ("ipod", "aac", "audio/mp4", "m4a"),
}


@functools.lru_cache(maxsize=2)
def executable(name):
    configured = os.environ.get(f"MMP_{name.upper()}", name)
    path = shutil.which(configured)
    if path is None:
        raise RuntimeError(f"dependency_unavailable: {name} executable is not available; configure MMP_{name.upper()}")
    return path


async def execute(cmd, *, capture=False, outputs=()):
    # File-backed diagnostics avoid holding arbitrary decoder logs in Python memory.
    with tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdout:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout if capture else asyncio.subprocess.DEVNULL, stderr=stderr)
        waiter = asyncio.create_task(proc.wait())
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), 0.1)
                    break
                except asyncio.TimeoutError:
                    if sum(path.stat().st_size for path in outputs if path.exists()) >= MAX_BYTES:
                        raise MediaError(f"resource_limit: combined audio output reaches {MAX_BYTES} bytes")
                    if os.fstat(stderr.fileno()).st_size > MAX_LOG_BYTES:
                        raise MediaError("resource_limit: decoder diagnostics exceed 2 MiB")
                    if capture and os.fstat(stdout.fileno()).st_size > MAX_LOG_BYTES:
                        raise MediaError("resource_limit: media metadata exceeds 2 MiB")
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await waiter
            raise
        size = stderr.tell()
        stderr.seek(max(0, size - MAX_LOG_BYTES))
        log = stderr.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
        if proc.returncode:
            reason = log[-2000:].strip()
            if any(s in log for s in ("Unknown encoder", "Encoder not found", "No such filter")):
                raise RuntimeError(f"dependency_unavailable: FFmpeg build lacks required codec/filter: {reason}")
            category = "unsupported_format" if "not on whitelist" in log else "invalid_media" if (
                "Invalid data found" in log or "Error while decoding" in log) else "processing_failed"
            raise MediaError(f"{category}: FFmpeg rejected the media or parameters: {reason}")
        if size > MAX_LOG_BYTES:
            raise MediaError("resource_limit: decoder diagnostics exceed 2 MiB")
        if capture:
            if stdout.tell() > MAX_LOG_BYTES:
                raise MediaError("resource_limit: media metadata exceeds 2 MiB")
            stdout.seek(0)
            return stdout.read().decode("utf-8"), log
        return "", log


def input_args(path, seek=None):
    # No playlists, network protocols, or demuxers that open arbitrary referenced files.
    return ["-threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
            *([] if seek is None else ["-ss", str(seek)]), "-i", str(path)]


def ffmpeg():
    return [executable("ffmpeg"), "-hide_banner", "-nostdin", "-nostats", "-y", "-xerror",
            "-filter_threads", "1", "-filter_complex_threads", "1"]


async def probe(path):
    raw, _ = await execute([
        executable("ffprobe"), "-v", "error", "-threads", "1",
        "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
        "-show_entries", "stream=codec_type,codec_name,sample_rate,channels,channel_layout,duration,bits_per_raw_sample,bits_per_sample:stream_disposition=attached_pic:format=format_name,duration",
        "-of", "json", str(path),
    ], capture=True)
    try:
        data = json.loads(raw)
        streams = data.get("streams", [])
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if len(audio) != 1:
            raise MediaError(f"unsupported_input: expected one audio stream, found {len(audio)}")
        if any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams):
            raise MediaError("unsupported_input: video container is outside audio tools; submit its audio track")
        stream = audio[0]
        duration = float(stream.get("duration") or data["format"].get("duration") or "nan")
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError("invalid_media: audio has no finite positive duration")
        if duration > MAX_DURATION:
            raise MediaError(f"resource_limit: duration {duration:g}s exceeds {MAX_DURATION}s")
        if not 1 <= channels <= 32 or not 1 <= rate <= 192000:
            raise MediaError("resource_limit: audio exceeds 32 channels or 192000 Hz")
        depth = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
        return {"format": data["format"]["format_name"], "codec": stream["codec_name"],
                "sample_rate": rate, "channels": channels, "duration_sec": duration,
                "bytes": Path(path).stat().st_size, "bit_depth": depth}
    except (ValueError, KeyError, TypeError) as e:
        if isinstance(e, MediaError):
            raise
        raise MediaError(f"invalid_media: incomplete audio metadata: {e}") from e


def encoder(opts, metadata):
    fmt = opts.get("format", "flac")
    muxer, codec, mime, extension = FORMATS[fmt]
    if "bit_depth" in opts and fmt not in {"wav", "flac"}:
        raise MediaError("invalid_argument: bit_depth is only valid for WAV/FLAC")
    if "bitrate_kbps" in opts and fmt in {"wav", "flac"}:
        raise MediaError("invalid_argument: bitrate_kbps is only valid for lossy formats")
    channels, rate = metadata["channels"], metadata["sample_rate"]
    if fmt in {"mp3", "opus"} and channels > 2:
        raise MediaError(f"unsupported_output: {fmt} output supports mono/stereo in this tool; convert channels first")
    if fmt == "mp3" and rate not in {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}:
        raise MediaError("unsupported_output: MP3 cannot preserve this sample rate; resample explicitly")
    if fmt == "opus" and rate != 48000:
        raise MediaError("unsupported_output: Opus output uses 48000 Hz; resample explicitly or choose another format")
    params = ["-c:a", codec, "-threads", "1", "-ar", str(rate)]
    if fmt in {"wav", "flac"}:
        depth = opts.get("bit_depth", "24" if metadata.get("bit_depth", 0) > 16 else "16")
        if fmt == "wav":
            params[1] = f"pcm_s{depth}le"
        else:
            params += ["-sample_fmt", "s32" if depth == "24" else "s16", "-compression_level", "2"]
    else:
        bitrate = opts.get("bitrate_kbps", 128 if fmt == "opus" else 192)
        if fmt == "opus" and bitrate > 256 * channels:
            raise MediaError("invalid_argument: Opus bitrate exceeds 256 kbps per channel")
        params += ["-b:a", f"{bitrate}k"]
    return params + ["-f", muxer], mime, extension


def interval(opts, duration):
    if "start_sec" not in opts or "end_sec" not in opts:
        raise MediaError("invalid_argument: start_sec and end_sec are required")
    start, end = opts["start_sec"], opts["end_sec"]
    if not 0 <= start < end <= duration:
        raise MediaError(f"invalid_argument: interval must satisfy 0 <= start < end <= {duration:g}s")
    return start, end


def duration_limit(duration):
    if duration > MAX_DURATION:
        raise MediaError(f"resource_limit: output duration {duration:g}s exceeds {MAX_DURATION}s")


def normalized(index, metadata, rate, channels):
    # A channel-count layout asks libswresample to perform the documented down/upmix.
    return f"[{index}:a:0]aresample={rate},aformat=sample_fmts=fltp:channel_layouts={channels}c,asetpts=PTS-STARTPTS"


async def process(job):
    op = job["task_type"].removeprefix("audio.")
    args, opts = job["arguments"], job["arguments"].get("options") or {}
    finite_options(opts)
    items = args["audio"] if isinstance(args["audio"], list) else [args["audio"]]
    paths = [Path(item["path"]) for item in items]
    infos = [await probe(path) for path in paths]
    info = infos[0]
    if op == "inspect":
        return {"metadata": {"kind": "json", "value": {"files": infos}}}
    rate, channels, duration = info["sample_rate"], info["channels"], info["duration_sec"]
    filters, graph = [], None
    seek, length, copied = None, None, False
    output_channels, output_rate = channels, rate
    channel_indices = [None]
    if op == "crop":
        seek, end = interval(opts, duration)
        length = end - seek
    elif op == "resample":
        output_rate = opts["sample_rate"]
        filters.append(f"aresample={output_rate}")
    elif op == "channels":
        mode = opts["mode"]
        if mode == "extract":
            if "channel" not in opts or opts["channel"] >= channels:
                raise MediaError(f"invalid_argument: channel must be in 0..{channels - 1}")
            channel_indices = [opts["channel"]]
            output_channels = 1
        else:
            if "channel" in opts:
                raise MediaError("invalid_argument: channel is only valid for mode=extract")
            if mode == "split":
                channel_indices, output_channels = list(range(channels)), 1
            else:
                output_channels = 1 if mode == "mono" else 2
                filters.append(f"aformat=channel_layouts={output_channels}c")
    elif op == "volume":
        filters.append(f"volume={opts['gain_db']}dB")
        if opts.get("clipping", "reject") == "limit":
            filters.append("alimiter=limit=1:level=false:latency=true")
    elif op == "normalize":
        target, peak = opts.get("lufs", -16), opts.get("true_peak_db", -1)
        _, log = await execute([*ffmpeg(), *input_args(paths[0]), "-map", "0:a:0", "-vn",
            "-af", "ebur128=peak=true:framelog=verbose", "-f", "null", "-"])
        loudness = re.findall(r"I:\s+([-+0-9.inf]+)\s+LUFS", log)
        true_peaks = re.findall(r"Peak:\s+([-+0-9.inf]+)\s+dBFS", log)
        if not loudness or not true_peaks:
            raise RuntimeError("processing_failed: FFmpeg did not return loudness measurements")
        measured_i, measured_tp = float(loudness[-1]), float(true_peaks[-1])
        if not math.isfinite(measured_tp) or measured_i <= -70:
            raise MediaError("invalid_media: silence or audio too short for finite LUFS measurements")
        gain = target - measured_i
        if measured_tp + gain + 0.1 > peak:
            raise MediaError("unsupported_operation: LUFS target conflicts with true-peak limit; choose a lower target")
        filters.append(f"volume={gain}dB")
    elif op == "fade":
        fade_in, fade_out = opts.get("in_sec", 0), opts.get("out_sec", 0)
        if fade_in + fade_out > duration:
            raise MediaError("invalid_argument: fade durations must not overlap or exceed the audio")
        if fade_in:
            filters.append(f"afade=t=in:st=0:d={fade_in}")
        if fade_out:
            filters.append(f"afade=t=out:st={duration - fade_out}:d={fade_out}")
    elif op in {"concat", "mix"}:
        graph_parts = []
        if op == "concat":
            gap, cross = opts.get("gap_sec", 0), opts.get("crossfade_sec", 0)
            if gap and cross:
                raise MediaError("invalid_argument: gap_sec and crossfade_sec are mutually exclusive")
            if cross and any(item["duration_sec"] <= cross for item in infos):
                raise MediaError("invalid_argument: crossfade must be shorter than every input")
            duration_limit(sum(item["duration_sec"] for item in infos) + (len(infos) - 1) * (gap - cross))
            for i, item in enumerate(infos):
                part = normalized(i, item, rate, channels)
                if gap and i < len(infos) - 1:
                    part += f",apad=pad_dur={gap}"
                graph_parts.append(f"{part}[s{i}]")
            if cross and len(infos) > 1:
                previous = "s0"
                for i in range(1, len(infos)):
                    current = f"c{i}"
                    graph_parts.append(f"[{previous}][s{i}]acrossfade=d={cross}:c1=tri:c2=tri[{current}]")
                    previous = current
                graph_parts.append(f"[{previous}]anull[out]")
            else:
                graph_parts.append("".join(f"[s{i}]" for i in range(len(infos))) + f"concat=n={len(infos)}:v=0:a=1[out]")
        else:
            offsets, gains = opts.get("offsets_sec", [0] * len(infos)), opts.get("gains_db", [0] * len(infos))
            if len(offsets) != len(infos) or len(gains) != len(infos):
                raise MediaError("invalid_argument: offsets_sec and gains_db must each match the number of inputs")
            duration_limit(max(item["duration_sec"] + offset for item, offset in zip(infos, offsets)))
            for i, (item, offset, gain) in enumerate(zip(infos, offsets, gains)):
                graph_parts.append(normalized(i, item, rate, channels) +
                    f",volume={gain}dB,adelay=delays={round(offset * rate)}S:all=1[s{i}]")
            graph_parts.append("".join(f"[s{i}]" for i in range(len(infos))) +
                f"amix=inputs={len(infos)}:duration=longest:dropout_transition=0:normalize={int(opts.get('normalize', False))}[out]")
        graph = ";".join(graph_parts)
    elif op == "silence":
        mode = opts["mode"]
        if mode == "pad":
            before, after = opts.get("before_sec", 0), opts.get("after_sec", 0)
            duration_limit(duration + before + after)
            filters.extend([f"adelay=delays={round(before * rate)}S:all=1", f"apad=pad_dur={after}"])
        elif mode == "mute":
            start, end = interval(opts, duration)
            # eval=frame volume expressions would mute complete frames. aeval is sample-exact.
            filters.append(f"aeval=exprs='if(gte(t,{start})*lt(t,{end}),0,val(ch))':c=same")
        else:
            threshold, minimum = opts.get("threshold_db", -50), opts.get("min_silence_sec", 0.1)
            _, log = await execute([*ffmpeg(), *input_args(paths[0]), "-map", "0:a:0", "-vn", "-af",
                f"silencedetect=noise={threshold}dB:d={minimum},"
                "astats=measure_perchannel=none:measure_overall=Number_of_samples", "-f", "null", "-"])
            counts = re.findall(r"Number of samples:\s+([0-9.]+)", log)
            if not counts:
                raise RuntimeError("processing_failed: FFmpeg did not report decoded sample count")
            decoded_duration = float(counts[-1]) / rate
            starts = [float(s) for s in re.findall(r"silence_start: ([0-9.e+-]+)", log)]
            ends = [float(s) for s in re.findall(r"silence_end: ([0-9.e+-]+)", log)]
            seek, end = 0, decoded_duration
            if starts and starts[0] <= 1 / rate and ends:
                seek = ends[0]
            if starts and ends and abs(ends[-1] - decoded_duration) <= 1 / rate + 0.000001:
                end = starts[-1]
            if end <= seek:
                raise MediaError("invalid_media: no audible samples remain after trimming silence")
            length = end - seek
    elif op == "convert":
        expected_codec = {"wav": {"pcm_s16le", "pcm_s24le"}, "flac": {"flac"}, "mp3": {"mp3"},
                          "ogg": {"vorbis"}, "opus": {"opus"}, "m4a": {"aac"}}[opts.get("format", "flac")]
        copied = info["codec"] in expected_codec and not any(k in opts for k in ("bit_depth", "bitrate_kbps"))
    else:
        raise MediaError(f"unsupported_operation: audio.{op}")

    output_info = {**info, "sample_rate": output_rate, "channels": output_channels}
    enc, mime, extension = encoder(opts, output_info)
    outputs, metadata = [], []
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="mmp-audio-") as directory:
        cmd = ffmpeg()
        for i, path in enumerate(paths):
            cmd += input_args(path, seek if i == 0 else None)
        destinations = []
        for index in channel_indices:
            destination = Path(directory) / f"output-{len(destinations)}.{extension}"
            destinations.append(destination)
            chain = list(filters)
            if index is not None:
                chain.insert(0, f"pan=mono|c0=c{index}")
            # Measure float peaks before integer encoding; fail instead of silently clipping.
            peak_filter = ("aformat=sample_fmts=fltp,astats=measure_perchannel=none:"
                           "measure_overall=Peak_level+Number_of_NaNs+Number_of_Infs")
            if graph:
                cmd += ["-filter_complex", graph.replace("[out]", "[pre]") + f";[pre]{peak_filter}[out]", "-map", "[out]"]
            else:
                cmd += ["-map", "0:a:0"]
                if not copied:
                    cmd += ["-af", ",".join(["aformat=sample_fmts=fltp", *chain, peak_filter])]
            if length is not None:
                cmd += ["-t", str(length)]
            actual_encoder = ["-c:a", "copy", "-f", FORMATS[opts.get("format", "flac")][0]] if copied else enc
            cmd += ["-vn", "-sn", "-dn", "-map_metadata", "-1", *actual_encoder,
                    "-fs", str(MAX_BYTES), str(destination)]
        # Mapping several output filters from one input decodes that input only once.
        _, log = await execute(cmd, outputs=destinations)
        invalid_samples = re.findall(r"Number of (?:NaNs|Infs):\s+([0-9.]+)", log)
        if any(float(count) > 0 for count in invalid_samples):
            raise MediaError("invalid_media: decoded audio contains NaN or infinite samples")
        peaks = [float(p) for p in re.findall(r"Peak level dB: ([-+0-9.inf]+)", log)]
        if peaks and max(peaks) > 0.001:
            raise MediaError("invalid_argument: output would clip; reduce gain, normalize mixing, or explicitly limit volume")
        for destination in destinations:
            size = destination.stat().st_size
            total_bytes += size
            if total_bytes >= MAX_BYTES:
                raise MediaError(f"resource_limit: combined audio output reaches {MAX_BYTES} bytes")
            actual = await probe(destination)
            if length is not None and actual["duration_sec"] + 1 / output_rate + 0.000001 < length:
                raise MediaError("invalid_media: decoded audio ended before the requested interval")
            if actual["channels"] != output_channels:
                raise MediaError("unsupported_output: encoder cannot preserve the requested channel count")
            if actual["sample_rate"] != output_rate:
                raise MediaError("unsupported_output: encoder cannot preserve the requested sample rate")
            metadata.append(actual)
            outputs.append({"kind": "media", "content_type": mime,
                            "data_base64": base64.b64encode(destination.read_bytes()).decode("ascii")})
    return {"audio": outputs if op == "channels" else outputs[0],
            "metadata": {"kind": "json", "value": {"files": metadata}}}
