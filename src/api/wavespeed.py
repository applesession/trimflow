import os


def get_wavespeed_api_key():
    return (os.getenv("WAVESPEED_API_KEY") or "").strip()


def _extract_output_url(output_item):
    if isinstance(output_item, str):
        return output_item
    if isinstance(output_item, dict):
        for key in ("url", "src", "output", "image"):
            value = output_item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def run_edit_prediction(model, payload, *, timeout_seconds=180, poll_interval_seconds=3):
    if not get_wavespeed_api_key():
        raise RuntimeError("WAVESPEED_API_KEY is not configured")

    try:
        import wavespeed
    except ImportError as exc:
        raise RuntimeError("wavespeed package is not installed. Run: pip install -r requirements.txt") from exc

    run_payload = dict(payload or {})
    run_payload.setdefault("enable_sync_mode", False)
    run_payload.setdefault("enable_base64_output", False)

    try:
        result = wavespeed.run(model, run_payload)
    except Exception as exc:
        raise RuntimeError(f"WaveSpeed SDK request failed: {exc!r}") from exc

    if not isinstance(result, dict):
        raise RuntimeError(f"WaveSpeed SDK returned unexpected result type: {type(result).__name__}")

    outputs = result.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError(f"WaveSpeed SDK returned no outputs: {result}")

    output_url = _extract_output_url(outputs[0])
    if not output_url:
        raise RuntimeError(f"WaveSpeed SDK output URL is missing: {result}")

    return {
        "prediction_id": result.get("id") or result.get("request_id"),
        "status": str(result.get("status") or "completed").strip().lower(),
        "output_url": output_url,
        "outputs": outputs,
        "raw": result,
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
    }
