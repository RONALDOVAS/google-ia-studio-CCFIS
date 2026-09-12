"""Runtime fallback for the CGD frequency route.

The CGD frequency screen exposes the actual attendance table on the /list
variant for some contracts. Keep the existing parser as the primary path and
retry the canonical list route only when the first page yields no records.
"""
import scraper

_original_extract_frequency = scraper.extract_frequency
_original_contract_bundle = scraper.contract_bundle


def _extract_frequency_with_list_fallback(page, cid):
    result = _original_extract_frequency(page, cid)
    if result.get("registros"):
        return result

    list_url = f"{scraper.CGD_URL.rstrip('/')}/contratos/frequencias/{cid}/list"
    current = page.url
    try:
        if scraper.open_page(page, list_url, "frequencia", f"frequencia_list_{cid}"):
            fallback = _original_extract_frequency(page, cid)
            if fallback.get("registros"):
                print(
                    f"[FREQUENCIA] cid={cid} rota_lista_capturada="
                    f"{len(fallback.get('registros') or [])} registros",
                    flush=True,
                )
                return fallback
    finally:
        if current and page.url != current:
            try:
                scraper.open_page(page, current, "frequencia", f"frequencia_retorno_{cid}")
            except Exception:
                pass
    return result


scraper.extract_frequency = _extract_frequency_with_list_fallback
print("PATCH_FREQUENCIA_LISTA=OK", flush=True)
