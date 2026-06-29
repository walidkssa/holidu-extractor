# Image legere pour l'extraction Airbnb (pyairbnb, pur HTTP, pas de navigateur).
# Booking (Playwright) reste best-effort: si non installe, le service renvoie
# un resultat partiel sans planter. Pour Booking complet, voir README (variante Playwright).

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pyairbnb beautifulsoup4

COPY main.py .

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
