#!/usr/bin/env python3
"""Execute a hatch-pet run manifest (jobs.json) against an image backend.

Walks the dependency-ordered jobs, generates each pending image, then
auto-extracts row strips into 192x208 cells via extract_row_strip.py.
Backends:
  gemini  - Google AI Studio image API (needs GEMINI_API_KEY env var)
  abl     - a local OpenAI-compatible diffusion server (SDXL-class), e.g.
            diffusion_server.py from the ABL stack. ABL_DIFFUSION_URL env var
            overrides the default http://127.0.0.1:8102. Uses each job's
            compact "sd_prompt" (SDXL CLIP truncates at ~77 tokens) and
            ignores reference images (no reference conditioning).
  flux    - local ComfyUI at http://127.0.0.1:8188 (needs a workflow template;
            see the NotImplementedError message for wiring instructions)

Usage:
  python drive_run.py --run-dir <dir-with-jobs.json> --backend gemini [--only JOB_ID]

After all jobs are complete, finish with compose_atlas.py / validate_atlas.py /
make_contact_sheet.py / render_previews.py per the hatch-pet skill.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

GEMINI_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


def gen_abl(prompt, ref_paths, out_path, job=None):
    url = os.environ.get("ABL_DIFFUSION_URL", "http://127.0.0.1:8102")
    job = job or {}
    prompt = job.get("sd_prompt") or prompt
    size = job.get("size") or ("1024x1024" if job.get("kind") == "base" else "1536x640")
    quality = "high" if job.get("kind") == "base" else "auto"
    body = json.dumps({"prompt": prompt, "n": 1, "size": size,
                       "quality": quality, "response_format": "b64_json"}).encode()
    req = urllib.request.Request(url + "/v1/images/generations", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2400) as r:
        resp = json.load(r)
    if "error" in resp:
        raise RuntimeError(f"diffusion server: {resp['error']}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(resp["data"][0]["b64_json"]))


def gen_gemini(prompt, ref_paths, out_path, job=None):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("ABL_GEMINI_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY (or ABL_GEMINI_KEY) is not set")
    parts = [{"text": prompt}]
    for rp in ref_paths:
        with open(rp, "rb") as f:
            parts.append({"inline_data": {"mime_type": "image/png",
                                          "data": base64.b64encode(f.read()).decode()}})
    body = json.dumps({"contents": [{"parts": parts}]}).encode()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={key}")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    for cand in resp.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            blob = part.get("inline_data") or part.get("inlineData")
            if blob and blob.get("data"):
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(blob["data"]))
                return
    raise RuntimeError(f"no image in Gemini response: {json.dumps(resp)[:400]}")


def gen_flux(prompt, ref_paths, out_path, job=None):
    raise NotImplementedError(
        "FLUX backend not wired yet. Plan: run ComfyUI on the local GPU, save a "
        "workflow template with a reference-image (Kontext/redux) node, then POST "
        "it to http://127.0.0.1:8188/prompt with the prompt + refs substituted, "
        "poll /history/<prompt_id>, and copy the output image to out_path. "
        "Implement in this function; everything else in the pipeline is ready."
    )


BACKENDS = {"gemini": gen_gemini, "abl": gen_abl, "flux": gen_flux}


def detect_chroma(image_path, fallback):
    """Diffusion models rarely hit the exact requested key color — sample the
    strip's corners and use their median as the actual chroma key."""
    try:
        from PIL import Image
        im = Image.open(image_path).convert("RGB")
        w, h = im.size
        pts = [im.getpixel(p) for p in
               [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3),
                (w // 2, 2), (w // 2, h - 3)]]
        med = tuple(sorted(c[i] for c in pts)[len(pts) // 2] for i in range(3))
        return "%02x%02x%02x" % med
    except Exception:
        return fallback


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--backend", required=True, choices=sorted(BACKENDS))
    ap.add_argument("--only", help="run just this job id")
    args = ap.parse_args()

    run = args.run_dir
    manifest_path = os.path.join(run, "jobs.json")
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    jobs = {j["id"]: j for j in manifest["jobs"]}
    chroma = manifest["pet"].get("chroma_key", "00ff00")
    gen = BACKENDS[args.backend]

    def ready(j):
        return (j["status"] != "complete"
                and all(jobs[d]["status"] == "complete" for d in j["depends_on"]))

    progressed = True
    while progressed:
        progressed = False
        for j in manifest["jobs"]:
            if args.only and j["id"] != args.only:
                continue
            if not ready(j):
                continue
            prompt = open(os.path.join(run, j["prompt"]), encoding="utf-8").read()
            refs = [os.path.join(run, r) for r in j.get("refs", [])]
            out = os.path.join(run, j["output"])
            print(f"[{j['id']}] generating ({args.backend}) -> {j['output']}")
            gen(prompt, refs, out, j)
            if j["kind"] in ("row-strip", "look-row", "cardinal-strip"):
                cells = os.path.join(run, "frames",
                                     "look" if j["kind"] == "look-row" else j["id"])
                report = os.path.join(run, "qa", f"{j['id']}.json")
                os.makedirs(os.path.dirname(report), exist_ok=True)
                rc = subprocess.call([sys.executable,
                                      os.path.join(SCRIPTS, "extract_row_strip.py"),
                                      out, "--expected-frames", str(j["frames"]),
                                      "--chroma-key", detect_chroma(out, chroma),
                                      "--output-dir", cells, "--json-out", report])
                if rc != 0:
                    print(f"[{j['id']}] extraction FAILED - inspect {report}; "
                          f"job left pending for regeneration")
                    continue
                if j["kind"] == "look-row":
                    # rename 00..07 to the degree names compose_atlas expects
                    # (000, 022.5, 045 ... zero-padded, halves keep one decimal)
                    start = 0.0 if j["id"].endswith("9") else 180.0
                    for i in range(8):
                        deg = start + i * 22.5
                        name = f"{int(deg):03d}" if deg == int(deg) else f"{deg:05.1f}"
                        src = os.path.join(cells, f"{i:02d}.png")
                        if os.path.exists(src):
                            os.replace(src, os.path.join(cells, name + ".png"))
            j["status"] = "complete"
            json.dump(manifest, open(manifest_path, "w", encoding="utf-8"), indent=2)
            progressed = True

    pending = [j["id"] for j in manifest["jobs"] if j["status"] != "complete"]
    print("all jobs complete" if not pending else f"pending: {', '.join(pending)}")


if __name__ == "__main__":
    main()
