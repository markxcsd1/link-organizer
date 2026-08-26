"""
game_bot — a Telegram bot that turns a shared game link (trailer, review, or store
page) into a fully-populated Notion "To Play" backlog entry.

Pipeline: link → page metadata → resolve the game (Steam / IGDB / Groq) →
enrich (store page, trailer, review, release date, genres, platforms) →
confirmation card with a hype rating → save to Notion.

The package is split by responsibility:

    config       env vars + shared constants
    text_utils   JSON / string helpers
    telegram     Telegram Bot API calls
    llm          Groq chat-completions client
    metadata     page-metadata fetching (+ Jina Reader for JS pages)
    mapping      genre / platform / date / title normalisation
    steam        Steam store API (appdetails + search)
    igdb         IGDB game database client
    discovery    store-page / review / trailer lookups
    pipeline     analyse_game_link — orchestrates the above into one game dict
    notion       persist a game to the To Play database
    bot          FastAPI app, webhook, and Telegram interaction handlers
"""
