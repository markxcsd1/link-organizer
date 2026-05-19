# Link Organizer Agent

An AI-powered agent that automatically classifies any shared link and saves it to the right Notion database — triggered from your iPhone via the iOS Share Sheet.

## How it works

1. Tap **Share** on any link in Safari, Maps, YouTube, Amazon, etc.
2. The iOS Shortcut sends the URL to this API
3. Claude AI classifies the link into a category
4. The link is saved to the matching Notion database automatically

## Categories

| Category | Examples |
|----------|---------|
| 📍 Locations | Google Maps, Apple Maps, restaurants, hotels |
| 🛍️ Products | Amazon, any shopping/e-commerce link |
| 🎬 Videos | YouTube, TikTok, Vimeo |
| 🍳 Recipes | Cooking blogs, recipe sites |
| 📖 Articles | Blog posts, news, Wikipedia |
| 📌 Other | Anything that doesn't fit above |

## Stack

- **Python** + **FastAPI** — API backend
- **Gemini 1.5 Flash** (Google, free tier) — link classification
- **Notion API** — storage
- **Vercel** — deployment
- **iOS Shortcuts** — mobile trigger

## Setup

### 1. Clone & configure environment

```bash
git clone https://github.com/markxcsd1/link-organizer
cp .env.example .env
```

Fill in `.env` with your API keys (see `.env.example`).

### 2. Deploy to Vercel

```bash
npm i -g vercel
vercel --prod
```

Add all variables from `.env.example` to your Vercel project environment variables.

### 3. Set up Notion

- Create a Notion integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
- Share each of the 6 databases with your integration
- Copy each database ID into your environment variables

### 4. iOS Shortcut

1. Open **Shortcuts** app → New Shortcut
2. Add **Get Contents of URL**:
   - URL: `https://your-vercel-url.vercel.app/api/classify`
   - Method: POST · Content Type: JSON
   - Body: `{ "url": "Shortcut Input" }`
3. Add **Show Notification** → value: `Dictionary Value` → key: `message`
4. Enable **Show in Share Sheet** in shortcut settings

## API

### `POST /api/classify`

```json
{ "url": "https://...", "note": "optional context" }
```

**Response:**
```json
{
  "ok": true,
  "message": "📍 Saved to Locations\nCafé Central",
  "category": "location",
  "name": "Café Central",
  "notion_url": "https://notion.so/..."
}
```

### `GET /api/health`

Returns `{ "ok": true }`.
