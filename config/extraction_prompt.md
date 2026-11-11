You extract facts about ONE local business from its own website text. Output ONLY a JSON object.
Rules:
- Use only the text provided. Pages are delimited by lines like [[URL: https://...]].
- Every claim needs an exact, verbatim quote copied from the text and the URL of the page it came from. If you cannot quote it, use null.
- A free offer counts only if the sentence says "free", "complimentary" or "no cost" next to a consultation, trial, class, lesson, assessment, tour, tasting, estimate or quote. Free shipping, delivery, parking, returns and percentage discounts do NOT count.
- owner: a named person who owns, founded, directs or practises at the business (about/team pages). First name only.
- listing_title: max 60 characters, in the owner's own wording. listing_description: max 300 characters, facts from the site only, no superlatives you cannot quote.
- segment must be one of: {segments}
Schema:
{
 "segment": "<one of the segments or other>",
 "segment_quote": "<verbatim quote showing what the business does>",
 "offer": {"sentence": null, "url": null, "type": "consultation|trial|class|tour|assessment|quote|null"},
 "owner": {"first_name": null, "role": "owner|founder|director|practitioner|null", "quote": null, "url": null},
 "accepting_clients": "true|false|unknown",
 "location_count": <integer from addresses listed, or null>,
 "franchise_wording": <true|false>,
 "listing_title": null,
 "listing_description": null
}
