"""Executor de compatibilidade do scraper CGD.

A descoberta e a coleta reais ficam centralizadas em scraper.py.
Este arquivo nao classifica contratos por cor e nao duplica a logica de listagem.
"""

import scraper


if __name__ == "__main__":
    scraper.main()
