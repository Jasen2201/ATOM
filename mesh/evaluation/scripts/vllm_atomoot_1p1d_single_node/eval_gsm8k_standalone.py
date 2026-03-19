#!/usr/bin/env python3
"""
Standalone GSM8K evaluation - no vLLM source dependency.
Downloads GSM8K dataset from HuggingFace and evaluates via OpenAI-compatible API.

Usage:
  python eval_gsm8k_standalone.py --host http://10.36.41.138 --port 8000 --model qwen3-235b
  python eval_gsm8k_standalone.py --host http://10.36.41.138 --port 8000 --model qwen3-235b --num-questions 50
"""

import argparse
import concurrent.futures
import json
import re
import sys
import time

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

# 5-shot examples from GSM8K paper
FEW_SHOT_EXAMPLES = [
    {
        "question": "There are 15 trees in the grove. Grove workers will plant trees in the grove today. After they are done, there will be 21 trees. How many trees did the grove workers plant today?",
        "answer": "There are 15 trees originally. Then there were 21 trees after some more were planted. So there must have been 21 - 15 = 6. The answer is 6.",
    },
    {
        "question": "If there are 3 cars in the parking lot and 2 more cars arrive, how many cars are in the parking lot?",
        "answer": "There are originally 3 cars. 2 more cars arrive. 3 + 2 = 5. The answer is 5.",
    },
    {
        "question": "Leah had 32 chocolates and her sister had 42. If they ate 35, how many pieces do they have left in total?",
        "answer": "Originally, Leah had 32 chocolates. Her sister had 42. So in total they had 32 + 42 = 74. After eating 35, they had 74 - 35 = 39. The answer is 39.",
    },
    {
        "question": "Jason had 20 lollipops. He gave Denny some lollipops. Now Jason has 12 lollipops. How many lollipops did Jason give to Denny?",
        "answer": "Jason started with 20 lollipops. Then he had 12 after giving some to Denny. So he gave Denny 20 - 12 = 8. The answer is 8.",
    },
    {
        "question": "Shawn has five toys. For Christmas, he got two toys each from his mom and dad. How many toys does he have now?",
        "answer": "Shawn started with 5 toys. If he got 2 toys each from mom and dad, then that is 4 more toys. 5 + 4 = 9. The answer is 9.",
    },
]


def load_gsm8k(num_questions=None):
    """Load GSM8K test set from HuggingFace datasets."""
    try:
        from datasets import load_dataset

        ds = load_dataset("openai/gsm8k", "main", split="test")
        if num_questions and num_questions < len(ds):
            ds = ds.select(range(num_questions))
        return list(ds)
    except ImportError:
        pass

    # Fallback: download JSONL directly
    import urllib.request

    url = "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
    print(f"Downloading GSM8K test set...", flush=True)
    data = []
    with urllib.request.urlopen(url) as resp:
        for line in resp:
            data.append(json.loads(line))
            if num_questions and len(data) >= num_questions:
                break
    return data


def extract_answer(text):
    """Extract the final numerical answer from model output."""
    # Look for "The answer is X" pattern
    match = re.search(r"[Tt]he answer is\s*[:\s]*\$?(-?[\d,]+(?:\.\d+)?)", text)
    if match:
        return match.group(1).replace(",", "")

    # Look for #### pattern (GSM8K format)
    match = re.search(r"####\s*(-?[\d,]+(?:\.\d+)?)", text)
    if match:
        return match.group(1).replace(",", "")

    # Look for boxed answer
    match = re.search(r"\\boxed\{(-?[\d,]+(?:\.\d+)?)\}", text)
    if match:
        return match.group(1).replace(",", "")

    # Last number in text
    numbers = re.findall(r"-?[\d,]+(?:\.\d+)?", text)
    if numbers:
        return numbers[-1].replace(",", "")

    return None


def extract_gold_answer(answer_text):
    """Extract gold answer from GSM8K format (#### N)."""
    match = re.search(r"####\s*(-?[\d,]+(?:\.\d+)?)", answer_text)
    if match:
        return match.group(1).replace(",", "")
    return None


def build_prompt(question):
    """Build few-shot prompt for GSM8K."""
    prompt = ""
    for ex in FEW_SHOT_EXAMPLES:
        prompt += f"Question: {ex['question']}\nAnswer: {ex['answer']}\n\n"
    prompt += f"Question: {question}\nAnswer:"
    return prompt


def query_model(base_url, model, prompt, max_tokens=512, temperature=0.0):
    """Query the model via OpenAI-compatible completions API."""
    url = f"{base_url}/v1/completions"
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stop": ["\n\nQuestion:"],
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["text"]
    except Exception as e:
        print(f"  API error: {e}", flush=True)
        return ""


def main():
    parser = argparse.ArgumentParser(description="Standalone GSM8K Evaluation")
    parser.add_argument("--host", type=str, default="http://127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", type=str, default="qwen3-235b")
    parser.add_argument("--num-questions", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--save-results", type=str, default=None)
    args = parser.parse_args()

    base_url = f"{args.host}:{args.port}"

    print("=" * 60, flush=True)
    print("GSM8K Standalone Evaluation", flush=True)
    print(f"  Server:     {base_url}", flush=True)
    print(f"  Model:      {args.model}", flush=True)
    print(f"  Questions:  {args.num_questions}", flush=True)
    print(f"  Max tokens: {args.max_tokens}", flush=True)
    print(f"  Workers:    {args.workers}", flush=True)
    print("=" * 60, flush=True)

    # Verify server is reachable (try /v1/models first, fall back to /health or /v1/completions)
    server_ok = False
    for check_path in ["/v1/models", "/health", "/"]:
        try:
            r = requests.get(f"{base_url}{check_path}", timeout=10)
            if r.status_code < 500:
                print(
                    f"Server OK (checked {check_path}, status={r.status_code})",
                    flush=True,
                )
                server_ok = True
                break
        except Exception:
            continue
    if not server_ok:
        print(f"ERROR: Cannot reach server at {base_url}", flush=True)
        sys.exit(1)

    # Load dataset
    print("Loading GSM8K dataset...", flush=True)
    dataset = load_gsm8k(args.num_questions)
    print(f"Loaded {len(dataset)} questions", flush=True)

    correct = 0
    invalid = 0
    total = len(dataset)
    total_tokens = 0
    t0 = time.time()

    def evaluate_one(item):
        question = item["question"]
        gold = extract_gold_answer(item["answer"])
        prompt = build_prompt(question)
        response = query_model(
            base_url, args.model, prompt, args.max_tokens, args.temperature
        )
        predicted = extract_answer(response)
        tokens = len(response.split())  # rough estimate
        return gold, predicted, tokens, response

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(evaluate_one, item): i for i, item in enumerate(dataset)
        }
        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            gold, predicted, tokens, response = future.result()
            total_tokens += tokens

            if predicted is None:
                invalid += 1
                is_correct = False
            else:
                try:
                    is_correct = abs(float(predicted) - float(gold)) < 1e-3
                except (ValueError, TypeError):
                    is_correct = predicted == gold

            if is_correct:
                correct += 1

            results.append(
                {
                    "idx": idx,
                    "gold": gold,
                    "predicted": predicted,
                    "correct": is_correct,
                }
            )

            done = len(results)
            if done % 5 == 0 or done == total:
                acc = correct / done if done > 0 else 0
                print(
                    f"  [{done}/{total}] accuracy={acc:.1%} (correct={correct}, invalid={invalid})",
                    flush=True,
                )

    elapsed = time.time() - t0
    accuracy = correct / total if total > 0 else 0
    invalid_rate = invalid / total if total > 0 else 0
    tps = total_tokens / elapsed if elapsed > 0 else 0

    print(flush=True)
    print("=" * 60, flush=True)
    print("Results", flush=True)
    print("=" * 60, flush=True)
    print(f"  Accuracy:       {accuracy:.4f} ({accuracy*100:.1f}%)", flush=True)
    print(f"  Correct:        {correct}/{total}", flush=True)
    print(f"  Invalid:        {invalid}/{total}", flush=True)
    print(f"  Total time:     {elapsed:.1f}s", flush=True)
    print(f"  ~Throughput:    {tps:.0f} tokens/s", flush=True)
    print("=" * 60, flush=True)

    if accuracy >= 0.75:
        print("PASS - Accuracy is in expected range", flush=True)
    elif accuracy >= 0.60:
        print("WARN - Accuracy is lower than expected", flush=True)
    else:
        print("FAIL - Accuracy is significantly below expected range", flush=True)

    if args.save_results:
        output = {
            "accuracy": accuracy,
            "correct": correct,
            "total": total,
            "invalid": invalid,
            "invalid_rate": invalid_rate,
            "total_output_tokens": total_tokens,
            "tokens_per_second": tps,
            "latency": elapsed,
            "model": args.model,
            "details": sorted(results, key=lambda x: x["idx"]),
        }
        with open(args.save_results, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Results saved to {args.save_results}", flush=True)


if __name__ == "__main__":
    main()
