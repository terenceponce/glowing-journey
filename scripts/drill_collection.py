"""Seed or delete the unrelated Qdrant collection used by the wrong-collection drill (docs/failure-drills.md).

Run it with `make drill-wrong-collection-on` / `-off`, which pipe this file into `python -` in the chatbot pod
(it has EMBEDDER_URL and QDRANT_URL):
  python - seed|delete <collection>
`seed` creates the collection (384 dimensions, cosine, like `docs`) with a few short passages about cooking,
gardening and travel, embedded by the real embedder, with the same payload fields as `docs` (source, title,
section, chunk_index, text). It's what a stale or foreign collection looks like: it answers every query with
something, just not with anything relevant. `delete` removes it. Both are idempotent, and neither touches `docs`.
"""

import os
import sys
import uuid

import httpx
from qdrant_client import QdrantClient, models

ID_NAMESPACE = uuid.UUID("d5c7a1f2-3b4e-4f60-8a91-2c3d4e5f6a7b")

PAGES = {
    "recipes/sourdough.md": ("Sourdough bread", [
        ("Starter", "A sourdough starter is flour and water left to ferment until wild yeast and bacteria make it bubble. Feed it equal weights of flour and water every day, and keep it somewhere warm. It is ready to bake with when it doubles in size a few hours after feeding."),
        ("Baking", "Mix the starter with flour, water and salt, fold the dough every half hour for two hours, then let it rise overnight in the fridge. Bake it in a preheated Dutch oven at 240 degrees Celsius for twenty minutes with the lid on and twenty minutes with it off."),
    ]),
    "recipes/pasta.md": ("Fresh pasta", [
        ("Dough", "Fresh pasta dough is one egg for every hundred grams of flour, kneaded for ten minutes until smooth and rested for half an hour under a cloth. Roll it thin enough to read a newspaper through, then cut it into ribbons."),
        ("Cooking", "Cook fresh pasta in plenty of well salted boiling water for two to three minutes. Save a cup of the cooking water, because the starch in it helps the sauce cling to the noodles."),
    ]),
    "recipes/soup.md": ("Vegetable soup", [
        ("Base", "Start a vegetable soup by sweating chopped onion, carrot and celery in olive oil for ten minutes. Add garlic, a spoonful of tomato paste, then stock and a bay leaf, and simmer until the vegetables are tender."),
    ]),
    "garden/tomatoes.md": ("Growing tomatoes", [
        ("Planting", "Start tomato seeds indoors six weeks before the last frost. Move the seedlings outside when nights stay above ten degrees Celsius, burying the stem up to the first leaves so it grows extra roots."),
        ("Care", "Water tomatoes deeply at the base, a few times a week, rather than a little every day. Pinch out the side shoots on tall varieties and tie the main stem to a stake as it grows."),
    ]),
    "garden/compost.md": ("Composting", [
        ("Balance", "A good compost heap balances green material, such as vegetable peelings and grass clippings, with brown material, such as dry leaves and cardboard. Turn it every few weeks and keep it as damp as a wrung-out sponge."),
    ]),
    "travel/lisbon.md": ("A weekend in Lisbon", [
        ("Getting around", "Lisbon is built on seven hills, so comfortable shoes matter. The yellow tram 28 climbs through Alfama, and the funiculars at Gloria and Bica save a steep walk. A day pass covers trams, buses and the metro."),
        ("Food", "Try pasteis de nata warm from the oven, grilled sardines in summer, and bacalhau cooked in a dozen ways. Most restaurants serve a cheap dish of the day at lunchtime."),
    ]),
    "travel/packing.md": ("Packing light", [
        ("Carry-on", "Pack for a week with one carry-on bag: three tops, two bottoms, one warm layer, a rain jacket and a pair of comfortable shoes. Roll clothes instead of folding them, and put liquids in a clear bag within easy reach."),
    ]),
    "fitness/marathon.md": ("Marathon training", [
        ("Plan", "A beginner marathon plan runs four days a week for sixteen weeks. Most runs are easy and conversational, with one long run that grows by about a mile each week until it reaches twenty miles, three weeks before race day."),
        ("Race day", "Eat a familiar breakfast three hours before the start, begin slower than your goal pace, and take on water and a gel every forty-five minutes. The last six miles are where the training pays off."),
    ]),
    "music/guitar.md": ("Learning guitar", [
        ("First chords", "Beginners can play hundreds of songs with four open chords: G, C, D and E minor. Practice changing between them slowly for ten minutes a day, and press just behind the fret to avoid buzzing."),
    ]),
    "home/coffee.md": ("Brewing coffee", [
        ("Pour over", "For a pour-over coffee, use sixty grams of medium-ground coffee per litre of water at ninety-five degrees Celsius. Wet the grounds and wait thirty seconds, then pour in slow circles until the scale shows the full amount."),
    ]),
}


def main() -> None:
    action, collection = sys.argv[1], sys.argv[2]
    if collection == os.environ.get("DOCS_COLLECTION", "docs"):
        sys.exit("refusing to touch the docs collection")
    qdrant = QdrantClient(url=os.environ["QDRANT_URL"])
    exists = qdrant.collection_exists(collection)
    if action == "delete":
        if exists:
            qdrant.delete_collection(collection)
        print(f"collection {collection}: {'deleted' if exists else 'not there'}")
        return
    texts, payloads = [], []
    for source, (title, sections) in PAGES.items():
        for i, (section, text) in enumerate(sections):
            texts.append(text)
            payloads.append({"source": source, "title": title, "section": section, "chunk_index": i, "text": text})
    resp = httpx.post(f"{os.environ['EMBEDDER_URL']}/embed", json={"texts": texts, "kind": "passage"}, timeout=120)
    resp.raise_for_status()
    body = resp.json()
    if not exists:
        qdrant.create_collection(collection, vectors_config=models.VectorParams(size=body["dim"], distance=models.Distance.COSINE))
    qdrant.upsert(collection, points=[
        models.PointStruct(id=str(uuid.uuid5(ID_NAMESPACE, f"{p['source']}#{p['chunk_index']}")), vector=v, payload=p)
        for p, v in zip(payloads, body["vectors"])
    ])
    print(f"collection {collection}: {'created' if not exists else 'updated'}, {qdrant.count(collection, exact=True).count} points "
          f"from {len(PAGES)} pages (docs is untouched)")


if __name__ == "__main__":
    main()
