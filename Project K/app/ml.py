"""로컬 LoRA 학습·평가. 자료·권한 확인 전 실행 차단, CPU/CUDA 선택, 동일 평가셋 비교."""
import importlib.util, platform, shutil


def env_status():
    has = lambda m: importlib.util.find_spec(m) is not None
    info = {"python": platform.python_version(), "torch_installed": has("torch"), "transformers_installed": has("transformers"),
            "peft_installed": has("peft"), "nvidia_smi_found": shutil.which("nvidia-smi") is not None,
            "cuda_available": False, "gpus": [], "device": "cpu"}
    if info["torch_installed"]:
        import torch
        info["cuda_available"] = bool(torch.cuda.is_available())
        info["gpus"] = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())] if info["cuda_available"] else []
        info["device"] = "cuda" if info["cuda_available"] else "cpu"
    info["cuda_status"] = ("CUDA 감지됨 — 실제 실행 테스트 전(CUDA 실제 실행 미검증)" if info["cuda_available"]
                           else "CUDA 사용 불가 → CPU 경로 · CUDA 실제 실행 미검증")
    info["accelerated_tasks"] = {"로컬 모델 실행": "미구현(설계 단계)", "문서 임베딩": "미구현 — 현재 검색은 임베딩 없는 BM25(CPU)",
                                 "LoRA 학습": "구현 — 자료·권한·환경 확인 후 실행, 실제 학습 미검증"}
    return info


def lora_readiness(t):
    env = env_status()
    checks = [
        {"item": "학습 데이터 존재", "result": "데이터 부족",
         "detail": "원본에 고객 의견·설문 자유서술 텍스트가 없다. mkt_returns_ms.reason은 4개 범주값(오배송·색상불만·불량·단순변심)뿐이라 분류 학습 대상이 아니다."},
        {"item": "데이터 사용 권한·학습 적합성", "result": "사용자 확인 필요", "detail": "고객사가 학습 사용을 허용한 데이터 목록이 설정에 없다."},
        {"item": "학습·평가 데이터 분리", "result": "데이터 부족", "detail": "분리할 데이터가 없음"},
        {"item": "학습 환경(torch·peft)", "result": "성공" if env["torch_installed"] and env["peft_installed"] else "환경 부족으로 미실행",
         "detail": f"torch={env['torch_installed']}, peft={env['peft_installed']}, cuda={env['cuda_available']}"},
    ]
    import json
    from .core import ROOT
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "data_ext" / "ml_runs").glob("*/result.json")]
    syn = max((r for r in runs if r["manifest"].get("synthetic")), key=lambda r: r["at"], default=None)
    checks.append({"item": "학습·평가 코드 경로(합성 자료)", "result": "성공" if syn else "미실행",
                   "detail": (f"[모의 처리] {syn['at']} · {syn['device']} · 학습 {len(syn['train_ids'])}행/평가 {len(syn['eval_ids'])}행 · 기본 모델 NLL {syn['base_loss']:.3f} → LoRA {syn['lora_loss']:.3f}. "
                              "합성 문답·무작위 초기화 모델이라 성능 향상 증거가 아니다." if syn else "python -m app.ml --make-synthetic --device cpu 로 실행")})
    checks.append({"item": "CUDA 실제 실행·CPU/GPU 비교", "result": "성공" if any(r["device"] == "cuda" for r in runs) else "환경 부족으로 미실행",
                   "detail": "CUDA 실행 기록 있음" if any(r["device"] == "cuda" for r in runs) else "이 PC에 NVIDIA GPU 없음. GPU 환경에서 --device cuda 또는 --compare-devices 로 실행(미검증)"})
    return {"status": "학습 보류", "stage": "학습·평가 코드 구현 / 합성 자료 CPU 실행 " + ("확인" if syn else "전") + " / 실제 업무 자료·모델 미등록 / CUDA 미검증", "checks": checks,
            "note": "LoRA를 적용했다는 이유로 성능 향상을 주장하지 않는다. 기본 모델 대비 평가 결과가 생기기 전까지 적용 여부는 '미적용'이다."}


def select_device(torch, requested="auto"):
    if requested not in ("auto", "cpu", "cuda"):
        raise ValueError("device는 auto/cpu/cuda 중 하나여야 함")
    available = torch.cuda.is_available()
    if requested == "cuda" and not available:
        raise ValueError("CUDA 사용 불가 — NVIDIA GPU와 CUDA용 torch를 확인하세요")
    return "cuda" if requested == "cuda" or requested == "auto" and available else "cpu"


def prepare(manifest_path):
    """입력은 data_ext 안에, 원본 연결은 원본 데이터 루트 안에 제한한다."""
    import csv, hashlib, json
    from pathlib import Path
    from .core import ROOT
    ext = (ROOT / "data_ext").resolve()

    def inside(path, base):
        p = (base / path).resolve()
        if not p.is_relative_to(base) or any(x in (".claude", ".codex", ".agents", ".gitignore") for x in p.parts):
            raise ValueError("허용 자료 경로 밖의 파일")
        return p

    path = inside(manifest_path, ext)
    m = json.loads(path.read_text(encoding="utf-8"))
    required = ("id", "purpose", "permission_source", "confirmed_by", "confirmed_at", "source", "reason", "dataset", "model", "model_permission_source")
    if any(not isinstance(m.get(k), str) or not m[k].strip() for k in required):
        raise ValueError("학습 목적·사용 권한·자료 확인자·출처·모델 사용 권한을 먼저 확인해야 함")
    if m.get("as_of") != "2026-07-01" or m.get("kind") != "참가자 가정" or m.get("confirmed") is not True:
        raise ValueError("업무 기준일·자료 종류·담당자 확인 필요")
    dataset, model = inside(m["dataset"], ext), inside(m["model"], ext)
    if not model.is_dir():
        raise ValueError("사용 권한을 확인한 로컬 기본 모델 디렉터리가 필요함")
    with dataset.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="|")
        rows = list(reader)
    required_row = ("id", "group_id", "split", "prompt", "response", "kind", "as_of", "source_file", "source_row", "reason")
    raw = (ROOT.parent / "01_참가자_배포용" / "04_원본데이터").resolve()
    ids, prompts, groups, splits = set(), {}, {}, {"train": [], "eval": []}
    source_lengths = {}
    for row in rows:
        if any(not row.get(k) or not row[k].strip() or row[k] == "NONE" for k in required_row):
            raise ValueError("학습 행의 식별자·원본 파일/행·생성 이유 등 필수값 누락")
        if row["id"] in ids or row["split"] not in splits:
            raise ValueError("중복 식별자 또는 잘못된 train/eval 분리")
        if row["kind"] not in ("원본 기록", "부서 주장·분석", "참가자 가정", "합성 관측값", "계산 결과") or row["as_of"] != m["as_of"]:
            raise ValueError("자료 종류 또는 적용 시점 불일치")
        src = inside(row["source_file"], raw)
        if src not in source_lengths:
            source_lengths[src] = len(src.read_text(encoding="utf-8-sig").splitlines())
        if not 1 <= int(row["source_row"]) <= source_lengths[src]:
            raise ValueError("원본 연결 행이 파일 범위를 벗어남")
        prompt = " ".join(row["prompt"].split()).casefold()
        for key, seen in ((prompt, prompts), (row["group_id"], groups)):
            if key in seen and seen[key] != row["split"]:
                raise ValueError("학습/평가 누출: 동일 질문 또는 그룹이 양쪽에 있음")
            seen[key] = row["split"]
        ids.add(row["id"])
        splits[row["split"]].append(row)
    if not all(splits.values()):
        raise ValueError("독립적인 학습·평가 행이 모두 필요함")
    return m, model, splits, {"manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest()}


def train(manifest_path, device="auto", epochs=1):
    """동일 평가셋의 응답 토큰 평균 NLL 비교. 실제 업무 정확도·성과 향상 판정과는 별개다."""
    import json, time, uuid, os
    from .core import ROOT, now
    m, model_path, splits, hashes = prepare(manifest_path)
    if not isinstance(epochs, int) or not 1 <= epochs <= 10:
        raise ValueError("epochs는 1~10 정수")
    os.environ["HF_HOME"] = str(ROOT / "data_ext" / "ml_cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import LoraConfig, TaskType, get_peft_model
    chosen = select_device(torch, device)
    torch.manual_seed(42)
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(str(model_path), local_files_only=True, trust_remote_code=False, use_safetensors=True).to(chosen)

    def encode(row):
        p = tokenizer.encode(row["prompt"], add_special_tokens=False)
        r = tokenizer.encode(row["response"], add_special_tokens=False)
        if not p or not r or len(p) + len(r) > 512:
            raise ValueError("질문·응답은 비어 있지 않아야 하며 합계 512 토큰 이하 필요(자동 잘림 없음)")
        return {"input_ids": torch.tensor([p + r], device=chosen),
                "labels": torch.tensor([[-100] * len(p) + r], device=chosen)}, len(r)

    encoded = {s: [encode(r) for r in rows] for s, rows in splits.items()}

    def evaluate():
        model.eval()
        loss, count = 0.0, 0
        with torch.no_grad():
            for batch, n in encoded["eval"]:
                value = model(**batch).loss
                if not torch.isfinite(value):
                    raise ValueError("평가 손실이 유한하지 않음")
                loss += value.item() * n
                count += n
        return loss / count

    base_loss = evaluate()
    model = get_peft_model(model, LoraConfig(task_type=TaskType.CAUSAL_LM, r=8, lora_alpha=16, lora_dropout=0.0, target_modules="all-linear"))
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=0.0002)
    if chosen == "cuda":
        torch.cuda.synchronize()
    start = time.perf_counter()
    model.train()
    for _ in range(epochs):
        for batch, _ in encoded["train"]:
            optimizer.zero_grad()
            loss = model(**batch).loss
            if not torch.isfinite(loss):
                raise ValueError("학습 손실이 유한하지 않음")
            loss.backward()
            optimizer.step()
    if chosen == "cuda":
        torch.cuda.synchronize()
    seconds = time.perf_counter() - start
    adapted_loss = evaluate()
    run_id = "lora-" + uuid.uuid4().hex
    output = ROOT / "data_ext" / "ml_runs" / run_id
    output.mkdir(parents=True)
    gpu = {"name": torch.cuda.get_device_name(0), "max_memory_bytes": torch.cuda.max_memory_allocated()} if chosen == "cuda" else None
    result = {"id": run_id, "as_of": m["as_of"], "at": now(), "kind": "계산 결과", "gpu": gpu,
              "mode": "모의 처리 — 합성 자료·무작위 초기화 모델(코드 경로 검증용, 성능 증거 아님)" if m.get("synthetic") else "실제 작동",
              "source": m["source"], "reason": m["purpose"], "manifest": m, **hashes,
              "device": chosen, "epochs": epochs, "seed": 42, "train_seconds": seconds,
              "torch": torch.__version__, "model": m["model"],
              "train_ids": [r["id"] for r in splits["train"]], "eval_ids": [r["id"] for r in splits["eval"]],
              "metric": "동일 평가 응답 토큰 평균 NLL(낮을수록 작음)", "base_loss": base_loss,
              "lora_loss": adapted_loss, "delta": adapted_loss - base_loss,
              "note": "합성 입력이면 모의 결과. NLL 비교만으로 업무 성능·시장 성과 개선을 주장하지 않음. 기본 모델 자동 교체 없음."}
    model.save_pretrained(str(output / "adapter"))
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def compare_devices(manifest_path, epochs=1):
    """같은 자료·시드로 CPU와 CUDA를 차례로 학습해 시간을 비교한다. NVIDIA GPU가 없으면 오류(대체 실행 없음)."""
    import torch
    select_device(torch, "cuda")
    cpu, gpu = train(manifest_path, "cpu", epochs), train(manifest_path, "cuda", epochs)
    return {"kind": "계산 결과", "cpu_run": cpu["id"], "cuda_run": gpu["id"], "gpu": gpu["gpu"],
            "cpu_train_seconds": cpu["train_seconds"], "cuda_train_seconds": gpu["train_seconds"],
            "speedup": cpu["train_seconds"] / gpu["train_seconds"], "cpu_lora_loss": cpu["lora_loss"], "cuda_lora_loss": gpu["lora_loss"],
            "note": "1회 측정이며 CUDA 첫 호출 준비 시간이 포함된다. 작은 모델에서는 GPU가 더 느릴 수 있다."}


SYNTHETIC_DIR = "ml_synthetic"
# (질문, 응답, 원본 행) — 원본 approval_authority_policy.md의 해당 행 문구를 옮긴 합성 문답. 실제 고객 의견·업무 기록이 아니다.
_SYNTHETIC_QA = [
    ("캠페인 확대안이 건별 한도 이하이면 승인권자는?", "마케팅팀 전결", 9), ("캠페인 확대안이 건별 한도를 넘으면?", "대표 승인과 그룹 보고", 9),
    ("기안 총액이 총액 한도를 넘으면?", "대표 승인과 그룹 보고", 9), ("covenant 하회 예상 구간의 신규 현금 약정은?", "금액과 종류 불문 CEO 또는 이사회", 11),
    ("하회 구간 규정과 평시 전결 중 우선하는 것은?", "하회 구간 규정이 우선", 11), ("하회 구간에 현금이 나가는 신규 약정의 승인 사항은?", "그룹 CEO 승인 사항", 18),
    ("승인 여부의 판단 기준은?", "실질 공헌이익 기준", 35), ("예상 GMV로 승인을 판단하는가?", "아니다 실질 공헌이익 기준", 35),
    ("미결정으로 기한을 넘기면?", "묵시적 승인과 같은 효과", 39), ("확정기한 전에 해야 할 일은?", "기한 전에 결정", 39),
]


def make_synthetic():
    """외부 내려받기 없이 코드 경로 검증용 합성 입력을 data_ext/ml_synthetic에 만든다(무작위 초기화 초소형 모델 + 합성 문답).
    평가 행은 학습 행과 다른 원본 행(그룹)만 쓴다."""
    import csv, json
    from tokenizers import Tokenizer, Regex, models, pre_tokenizers
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast
    import torch
    from .core import ROOT
    out = ROOT / "data_ext" / SYNTHETIC_DIR
    (out / "model").mkdir(parents=True, exist_ok=True)
    src = "00_common/approval_authority_policy.md"
    rows = [{"id": f"SYN-{i:02d}", "group_id": f"policy-row-{line}", "split": "eval" if line in (35, 39) else "train", "prompt": q, "response": a,
             "kind": "합성 관측값", "as_of": "2026-07-01", "source_file": src, "source_row": line,
             "reason": "LoRA 학습·평가 코드 경로 검증용 합성 문답(원본 행 문구 기반, 실제 고객 의견 아님)"} for i, (q, a, line) in enumerate(_SYNTHETIC_QA, 1)]
    with (out / "rows.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter="|")
        w.writeheader()
        w.writerows(rows)
    vocab = {c: i for i, c in enumerate(["[UNK]"] + sorted({c for r in rows for c in r["prompt"] + r["response"]}))}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Split(Regex("."), "isolated")
    PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="[UNK]").save_pretrained(str(out / "model"))
    torch.manual_seed(42)
    GPT2LMHeadModel(GPT2Config(vocab_size=len(vocab), n_positions=128, n_embd=32, n_layer=2, n_head=2, bos_token_id=0, eos_token_id=0)).save_pretrained(str(out / "model"))
    manifest = {"id": "SYN-ML-01", "synthetic": True, "purpose": "LoRA 학습/평가 분리·기본 모델 비교 코드 경로 검증(합성). 업무 성능 검증 아님",
                "confirmed": True, "confirmed_by": "참가자(합성 검증 한정 — 실제 자료·권한 담당자 확인 아님)", "confirmed_at": "2026-07-01",
                "as_of": "2026-07-01", "kind": "참가자 가정", "permission_source": "합성 자료 — 고객 데이터 미사용",
                "source": src, "reason": "T9 실제 자료 확인 전 학습 코드가 도는지 확인", "dataset": f"{SYNTHETIC_DIR}/rows.csv", "model": f"{SYNTHETIC_DIR}/model",
                "model_permission_source": "무작위 초기화 모델을 로컬에서 생성 — 외부 모델 미사용"}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"{SYNTHETIC_DIR}/manifest.json"


if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", help="data_ext 기준 확인 완료 manifest JSON 경로")
    parser.add_argument("--make-synthetic", action="store_true", help="합성 검증 입력을 만들고 그 manifest로 실행")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--compare-devices", action="store_true", help="CPU와 CUDA 학습 시간 비교(NVIDIA GPU 필요)")
    args = parser.parse_args()
    manifest = make_synthetic() if args.make_synthetic else args.manifest
    if not manifest:
        parser.error("manifest 경로 또는 --make-synthetic 필요")
    run = compare_devices(manifest, args.epochs) if args.compare_devices else train(manifest, args.device, args.epochs)
    print(json.dumps(run, ensure_ascii=False, indent=2))
