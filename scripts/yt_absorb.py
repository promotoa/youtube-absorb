#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yt_absorb.py — 유튜브 영상 흡수 파이프라인의 기계 구간 (youtube-absorb 스킬의 짝)

사용:
  python yt_absorb.py <URL>                  # 기본: 자막 전사(무료·API 키 불필요) + 프레임
  python yt_absorb.py <URL> --light          # 전사만(프레임 생략)
  python yt_absorb.py <URL> --whisper        # Whisper API 전사(OPENAI_API_KEY 필요·정밀 모드)
  python yt_absorb.py <URL> --outdir <dir>   # 산출 위치(기본 ./yt-archive)

산출:  <outdir>/YYYY-MM-DD-<slug>/
         transcript.md   전사 전문([mm:ss] 앵커)
         frames_raw/     장면전환 프레임 후보(판독 후 선별 → frames/ 이동·raw 삭제)
         sheet_*.jpg     콘택트 시트(후보 30장 격자 — 저해상도 전수 판독용)
         frames_index.md 프레임 ↔ 타임스탬프 매핑
         meta.json       출처 메타(URL·채널·제목·게시일·길이)

요구사항:  yt-dlp(최신), ffmpeg. --whisper 모드는 curl + 환경변수 OPENAI_API_KEY.

정책(중요):
  - 영상·오디오 원본은 보관하지 않는다 — 전사 전문 + 선별 프레임만 남긴다.
  - 사적 아카이브·개인 분석 전용. 전사문을 블로그 등 공개물에 재게시하지 말 것
    (짧은 인용 + 출처 표기까지만).
"""
import argparse, concurrent.futures, json, os, re, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CHUNK_SEC = 600  # Whisper 청크 10분(25MB 업로드 상한 대비 ≈2.4MB)
YTDLP = ["yt-dlp", "--remote-components", "ejs:github",
         "--extractor-args", "youtube:player_client=default"]

def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if r.returncode != 0:
        raise SystemExit(f"명령 실패({r.returncode}): {' '.join(cmd[:3])}…\n{r.stderr[-800:]}")
    return r

def slugify(t, maxlen=40):
    t = re.sub(r"[\\/:*?\"<>|#%&{}$!@`+=~\s]+", "-", t).strip("-")
    return t[:maxlen].rstrip("-") or "video"

def mmss(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

_VFR = None
def vfr_flag():
    """장면추출 프레임레이트 플래그 — ffmpeg 9.0이 -vsync를 제거해 -fps_mode(5.0+)로 대체(실측 §4)."""
    global _VFR
    if _VFR is None:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        _VFR = ["-fps_mode", "vfr"] if "fps_mode" in (r.stdout or "") else ["-vsync", "vfr"]
    return _VFR

# ── 프레임 층 ──────────────────────────────────────────────────────────────

def dhash(path):
    """지각 해시(순수 stdlib) — ffmpeg로 9x8 그레이 raw(72B)를 뽑아 64비트 dHash."""
    r = subprocess.run(["ffmpeg", "-v", "quiet", "-i", path, "-vf", "scale=9:8", "-pix_fmt", "gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    b = r.stdout
    if len(b) < 72:
        return None
    h = 0
    for y in range(8):
        for x in range(8):
            h = (h << 1) | (1 if b[y * 9 + x] > b[y * 9 + x + 1] else 0)
    return h

def dedup_frames(fr, pts):
    """근사 중복 접기 — 화자↔같은 슬라이드 왕복 컷이 만드는 반복 프레임 제거(해밍 ≤6)."""
    files = sorted(f for f in os.listdir(fr) if f.endswith(".jpg"))
    kept, kept_pts, hashes = [], [], []
    for i, f in enumerate(files):
        h = dhash(os.path.join(fr, f))
        if h is not None and any(bin(h ^ k).count("1") <= 6 for k in hashes):
            os.remove(os.path.join(fr, f))
            continue
        if h is not None:
            hashes.append(h)
        kept.append(f)
        kept_pts.append(pts[i] if i < len(pts) else None)
    return kept, kept_pts

def contact_sheets(fr, kept, dest):
    """콘택트 시트 — 후보 전수를 30장 격자로 묶어 저해상도로 전수 판독하게 한다."""
    per, n = 30, 0
    for s in range(0, len(kept), per):
        batch = kept[s:s + per]
        lst = os.path.join(fr, f"_sheet{s//per}.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for b in batch:
                # concat 목록의 상대경로는 목록 파일 기준으로 해석된다 — 절대경로+슬래시로 고정
                f.write(f"file '{os.path.abspath(os.path.join(fr, b)).replace(os.sep, '/')}'\nduration 0.04\n")
        sheet = os.path.join(dest, f"sheet_{s//per:02d}.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0", "-i", lst,
                        "-vf", "scale=320:-2,tile=5x6", "-frames:v", "1", sheet], capture_output=True)
        os.remove(lst)
        if os.path.exists(sheet):  # 개수는 실제 생성된 파일만 센다
            n += 1
    return n

# ── 전사 층 A: 자막(기본 — 무료·API 키 불필요) ─────────────────────────────

def parse_vtt(path):
    """VTT → (초, 텍스트) 세그먼트. 자동 자막의 롤링 중복(같은 줄이 두 큐에 걸침)을 접는다."""
    cues, t, buf = [], None, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip("﻿\r\n")
            m = re.match(r"(\d+):(\d{2}):(\d{2})[.,]\d+\s*-->", line)
            if m:
                if t is not None and buf:
                    cues.append((t, buf))
                t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
                buf = []
            elif line and "-->" not in line and not line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
                txt = re.sub(r"<[^>]+>", "", line).strip()
                if txt and not re.fullmatch(r"\d+", txt):
                    buf.append(txt)
    if t is not None and buf:
        cues.append((t, buf))
    segs, last = [], None
    for t, lines in cues:
        for ln in lines:
            if ln != last:
                segs.append((t, ln))
                last = ln
    return segs

def transcribe_subs(tmp, url, dur, langs=("ko", "en", "ja")):
    """수동 자막 우선, 없으면 자동 자막. 언어별 개별 요청 — 한 언어의 429/부재가 전체를 죽이지 않게.
    langs = 시도 순서(기본 ko→en→ja · 호출부가 메타의 영상 원어·실재 자막 언어를 뒤에 붙인다)."""
    for flag, label in (("--write-subs", "수동 자막"), ("--write-auto-subs", "자동 자막")):
        for lang in langs:
            subprocess.run(YTDLP + [flag, "--sub-langs", lang, "--sub-format", "vtt", "--skip-download",
                                    "-o", os.path.join(tmp, "subs.%(ext)s"), url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
            got = [f for f in os.listdir(tmp) if f.startswith("subs.") and f.endswith(".vtt")]
            if got:
                segs = parse_vtt(os.path.join(tmp, got[0]))
                if segs:
                    return segs, f"{label}({lang})"
                for g in got:
                    os.remove(os.path.join(tmp, g))
    raise SystemExit("자막을 받지 못했습니다 — 영상에 자막이 없거나, 시도한 언어(" + ",".join(langs) + ") 밖의 "
                     "언어이거나, 유튜브가 요청을 제한(429)했을 수 있습니다. `yt-dlp --list-subs <URL>`로 실재 "
                     "자막 언어를 먼저 확인하세요(있으면 무료 경로가 살아 있는 것) — 정말 자막이 없을 때만 "
                     "--whisper 모드(OPENAI_API_KEY 필요·유료)입니다.")

# ── 전사 층 B: Whisper API(--whisper · 정밀 모드) ──────────────────────────

def transcribe_chunk(key, path, offset):
    """curl 멀티파트(파이썬 의존성 0) → verbose_json 세그먼트에 청크 오프셋을 더해 반환."""
    r = sh(["curl", "-s", "--max-time", "300",
            "-H", f"Authorization: Bearer {key}",
            "-F", f"file=@{path}", "-F", "model=whisper-1",
            "-F", "response_format=verbose_json",
            "https://api.openai.com/v1/audio/transcriptions"])
    d = json.loads(r.stdout)
    if "segments" not in d:
        raise SystemExit(f"Whisper 응답 이상({os.path.basename(path)}): {r.stdout[:300]}")
    return [(s["start"] + offset, (s["text"] or "").strip()) for s in d["segments"]]

def transcribe_whisper(tmp, url, dur):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("--whisper 모드는 환경변수 OPENAI_API_KEY가 필요합니다.")
    sh(YTDLP + ["-f", "bestaudio", "-o", os.path.join(tmp, "audio.%(ext)s"), "--no-playlist", url])
    audio = next(os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("audio."))
    sh(["ffmpeg", "-y", "-i", audio, "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(CHUNK_SEC), os.path.join(tmp, "chunk_%03d.mp3")])
    chunks = sorted(f for f in os.listdir(tmp) if f.startswith("chunk_"))
    segs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(transcribe_chunk, key, os.path.join(tmp, c), i * CHUNK_SEC)
                for i, c in enumerate(chunks)]
        for f in futs:
            segs.extend(f.result())
    segs.sort(key=lambda x: x[0])
    return segs, f"Whisper API(청크 {len(chunks)} · 예상 비용 ≈ ${dur/60*0.006:.2f})"

# ── 메인 ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--light", action="store_true", help="프레임 추출 생략(전사만)")
    ap.add_argument("--whisper", action="store_true", help="Whisper API 전사(OPENAI_API_KEY 필요)")
    ap.add_argument("--outdir", default="yt-archive")
    a = ap.parse_args()

    # ① 메타
    meta = json.loads(sh(YTDLP + ["-J", "--no-download", a.url]).stdout)
    title, vid = meta.get("title", "video"), meta.get("id", "unknown")
    up = meta.get("upload_date", "00000000")
    updated = f"{up[:4]}-{up[4:6]}-{up[6:8]}" if len(up) == 8 else up
    dur = int(meta.get("duration") or 0)
    dest = os.path.join(a.outdir, f"{updated}-{slugify(title)}")
    os.makedirs(dest, exist_ok=True)
    print(f"① 메타 OK — {title} · {meta.get('channel','?')} · {mmss(dur)} · → {dest}")

    with tempfile.TemporaryDirectory(prefix="ytabsorb_") as tmp:
        # ② 전사(기본 = 자막·무료 / --whisper = 정밀)
        if a.whisper:
            segs, how = transcribe_whisper(tmp, a.url, dur)
        else:
            # 시도 언어 = ko/en/ja + 영상 원어 + 실재하는 수동 자막 언어(메타에서 도출 — 3언어 밖 영상도 무료 경로 유지)
            langs = ["ko", "en", "ja"]
            for cand in ([str(meta.get("language") or "").split("-")[0]]
                         + list((meta.get("subtitles") or {}).keys())[:5]):
                base = cand.strip()
                if base and base not in langs:
                    langs.append(base)
            segs, how = transcribe_subs(tmp, a.url, dur, langs=tuple(langs[:8]))
        print(f"② 전사 OK — {how} · 세그먼트 {len(segs)}")

        # ③ 프레임 후보(장면전환 · --light면 생략)
        n_frames, sheets = 0, 0
        if not a.light:
            # 영상 스트림만 403이 나도(오디오·자막은 성공) 전사를 인질로 잡지 않는다 — light 강등
            try:
                sh(YTDLP + ["-f", "bv*[height<=480]/bv*", "-o", os.path.join(tmp, "video.%(ext)s"),
                            "--no-playlist", a.url])
            except SystemExit as e:
                print(f"   ⚠️영상 스트림 실패 — 프레임 층 포기·전사만 착지(light 강등): {str(e)[:300]}")
                a.light = True
        if not a.light:
            video = next(os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("video."))
            fr = os.path.join(dest, "frames_raw")
            for thresh in ("0.30", "0.45"):
                shutil.rmtree(fr, ignore_errors=True); os.makedirs(fr)
                r = subprocess.run(["ffmpeg", "-y", "-i", video,
                                    "-vf", f"select='gt(scene,{thresh})',showinfo", *vfr_flag(),
                                    os.path.join(fr, "f_%04d.jpg")],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
                pts = re.findall(r"pts_time:([0-9.]+)", r.stderr)
                n_frames = len([f for f in os.listdir(fr) if f.endswith(".jpg")])
                if r.returncode != 0 and n_frames == 0:
                    # 조용한 0장 금지 — 옵션 파스 에러 등은 소리를 내야 고칠 수 있다(§4)
                    print(f"   ⚠️ffmpeg 장면추출 실패(rc={r.returncode}): {r.stderr.strip()[-300:]}")
                    break
                if n_frames <= 150:
                    break
                print(f"   프레임 {n_frames} > 150 — 문턱 {thresh}→상향 재추출")
            kept, kept_pts = dedup_frames(fr, pts)
            sheets = contact_sheets(fr, kept, dest)
            with open(os.path.join(dest, "frames_index.md"), "w", encoding="utf-8") as f:
                f.write(f"# 프레임 ↔ 타임스탬프 (장면전환 {n_frames} → dHash 접기 후 **{len(kept)}장** · "
                        f"콘택트 시트 {sheets}장 — 시트 전수 판독 → 선별만 원본 정독·frames/ 이동·raw 삭제)\n\n")
                for i, (fn, p) in enumerate(zip(kept, kept_pts)):
                    ts = mmss(float(p)) if p else "?"
                    f.write(f"- {fn} — [{ts}] · 시트 sheet_{i//30:02d} 칸 {i%30+1}\n")
            n_frames = len(kept)
        print(f"③ 프레임 후보 {n_frames}장" + (" (light — 생략)" if a.light else f" · 콘택트 시트 {sheets}장"))

    # ④ transcript.md + meta.json
    with open(os.path.join(dest, "transcript.md"), "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"> 출처: {meta.get('channel','?')} · {updated} · {mmss(dur)} · {a.url}\n")
        f.write(f"> 전사: {how} · 상태: **기계 전사 — 프레임 교정 전**\n")
        f.write(f"> ⚠️사적 아카이브 전용 — 전사문의 공개 재게시 금지(짧은 인용+출처만)\n\n")
        for t, txt in segs:
            if txt:
                f.write(f"`[{mmss(t)}]` {txt}\n\n")
    with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"url": a.url, "id": vid, "title": title, "channel": meta.get("channel"),
                   "upload_date": updated, "duration_sec": dur, "transcribed_by": how,
                   "segments": len(segs), "frames_kept": n_frames, "light": a.light},
                  f, ensure_ascii=False, indent=1)
    print(f"④ 완료 — {dest} (transcript.md · 선별 프레임 {n_frames}장)")
    print("다음 = Claude 판독: 시트 전수 판독 → 표적 교정(고유명사·명령어·수치) → 프레임 선별(frames/) → raw 삭제 → 채팅 보고(SKILL.md §2 양식) → 다음 스텝 제안(§3)")

if __name__ == "__main__":
    main()
