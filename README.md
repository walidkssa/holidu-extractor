# holidu-extractor

Microservice **portable et optionnel** d'extraction pour le générateur de Fiches Holidu
(portfolio `walidkoussa.com`). Il tourne sur n'importe quelle machine que vous contrôlez
(laptop, Raspberry Pi, petit VPS) et s'expose via un tunnel gratuit. Le portfolio s'y
connecte uniquement via la variable `EXTRACTOR_URL` : aucune dépendance à une IP précise.

> Sans ce service, l'outil reste **pleinement utilisable** : saisie manuelle, upload de
> photos, et dates déduites automatiquement des liens collés. Ce service ajoute seulement
> l'auto-remplissage **prix + photos** depuis les liens Airbnb / Booking.

## Pourquoi un service séparé (pas sur Vercel)

Le scraping ne fonctionne pas sur Vercel : IP datacenter bloquée par Airbnb/Booking, et
Python + navigateur headless incompatibles avec le serverless. On l'isole donc ici, et on
peut le couper ou le remplacer en un seul point (`EXTRACTOR_URL`).

## Déploiement en un clic (pour activer le « 100 % auto » avec vrais prix)

C'est l'étape qui permet à l'outil de récupérer **les vrais prix Airbnb pour les
dates** (et photos, avis, note) automatiquement, sans que Romane ne tape quoi que ce soit.

**Option A : Render (gratuit, recommandé)**
1. Pousser ce dossier sur un repo GitHub.
2. [render.com](https://render.com) > New > Blueprint > choisir le repo (le `render.yaml` est détecté).
3. Définir `EXTRACTOR_SECRET` (une valeur secrète au choix).
4. Récupérer l'URL publique (ex. `https://holidu-extractor.onrender.com`).
5. Côté portfolio Vercel > Settings > Environment Variables, ajouter :
   - `EXTRACTOR_URL` = l'URL Render
   - `EXTRACTOR_SECRET` = la même valeur qu'à l'étape 3
6. Redéployer le portfolio. C'est tout : coller les liens suffit désormais.

**Option B : Docker (n'importe quel host / ta machine)**
```bash
docker build -t holidu-extractor .
docker run -p 8000:8000 -e EXTRACTOR_SECRET=monsecret holidu-extractor
```
Puis exposer (tunnel) et renseigner `EXTRACTOR_URL` côté Vercel (voir plus bas).

> Cette image fait l'**Airbnb** (pyairbnb, sans navigateur). Pour **Booking** complet,
> installer Playwright en plus (voir la section Installation). Sans extracteur, l'outil
> reste utilisable : noms, photos, période et textes sont automatiques, seuls les prix
> sont à compléter (jamais inventés).

## Installation

```bash
cd holidu-extractor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # pour Booking
```

## Lancer

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Vérifier : `curl localhost:8000/health` → `{"ok": true}`.

## Exposer via un tunnel gratuit

```bash
# Cloudflare (recommandé, gratuit, sans compte pour un tunnel éphémère)
cloudflared tunnel --url http://localhost:8000
# ou ngrok http 8000   |   ou tailscale funnel 8000
```

Le tunnel donne une URL publique (ex. `https://xxxx.trycloudflare.com`).

## Brancher au portfolio

Dans les variables d'environnement Vercel (ou `.env.local`) du portfolio :

```
EXTRACTOR_URL=https://xxxx.trycloudflare.com
EXTRACTOR_SECRET=un-secret-partage   # optionnel mais recommandé
```

Si `EXTRACTOR_SECRET` est défini ici, définissez la **même** valeur côté service
(variable d'env `EXTRACTOR_SECRET`) : le service vérifie l'en-tête `x-secret`.

## API

`POST /extract` body `{ "url": "https://www.airbnb.fr/rooms/123?check_in=2026-06-15&check_out=2026-06-21" }`

Réponse (`ExtractResult`) :

```json
{
  "ok": true, "partial": false, "platform": "airbnb",
  "name": "...", "price": 2151, "currency": "EUR",
  "checkIn": "2026-06-15", "checkOut": "2026-06-21", "nights": 6,
  "surface": "98 m²", "rating": "9,5", "reviewsCount": "19",
  "location": "Cannes", "photos": ["https://..."]
}
```

Le service ne crash jamais : en cas d'échec il renvoie `{ ok:false, partial:true, photos:[] }`
avec au minimum les dates calculées depuis l'URL.

## Notes

- **Airbnb** : via [`pyairbnb`](https://github.com/johnbalvin/pyairbnb) (MIT, intercepte
  l'API GraphQL interne). Projet solo-maintainer : si la signature change, vérifier le
  README courant et adapter `extract_airbnb` (mapping défensif déjà en place).
- **Booking** : Playwright headful, best-effort (JSON-LD, `og:*`, prix visible). Pour plus
  de furtivité, utiliser Patchright (corrige `navigator.webdriver`) ou playwright-extra +
  stealth ; sur Linux sans écran, lancer derrière `xvfb-run`.
- **CGU** : scraper Airbnb/Booking est contraire à leurs conditions. Holidu étant
  partenaire, valider l'usage interne avant production.
