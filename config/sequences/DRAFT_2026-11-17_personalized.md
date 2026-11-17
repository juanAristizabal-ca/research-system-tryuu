---
approved: false          # drafts for the owner: nothing is sent or loaded until the owner edits this and sets approved: true
version: draft-1
---
# Draft messages (2026-11-17). Variables must come from verified data only.

Variables: Business_Name, City, Business_Type (Maps), Offer_Quote / Offer_Type (verbatim sentence found on the business's own site,
a statement, never a question or button), Opening_Quote (verbatim phrase with a 2025/2026 year or a licence issued in the last 12 months),
Peer_1 / Peer_2 (live Tryuu businesses of the same type, config/peers.yaml). No owner first names (unreliable).

## A. Free offer already on their site
Subject: Your free {{Offer_Type}} at {{Business_Name}}

Hi {{Business_Name}},

I read on your site: "{{Offer_Quote}}"

That is exactly what people look for on Tryuu, a local guide to Vancouver businesses. {{Peer_1}} and {{Peer_2}} already list theirs.

Want us to list yours, free? Reply "yes" and I'll send a preview built from your own site to approve first. Nothing goes live until you say so.

{{sendingAccountFirstName}} {{sendingAccountLastName}}
Tryuu +1 (672) 380 9940

Follow-up (day 3), subject "Re: ...": Hi {{Business_Name}}, quick follow-up. The preview of your {{Offer_Type}} takes two minutes to look over and it is free. Reply "yes" and I'll send it today.

## B. Recently opened
Subject: Congrats on {{Business_Name}}

Hi {{Business_Name}},

Your site mentions "{{Opening_Quote}}". Congratulations. New businesses in {{City}} gain the most from being found early on Tryuu, a free local guide where {{Peer_1}} and {{Peer_2}} already invite new customers.

Want us to list {{Business_Name}} with a welcome offer of your choice? Reply "yes" and I'll send a preview built from your site to approve first.

{{sendingAccountFirstName}} {{sendingAccountLastName}}
Tryuu +1 (672) 380 9940

## C. Control (the question style that worked best so far)
Subject: Free intro at {{Business_Name}}?

Hi {{Business_Name}},

Do you offer a free first class, trial or consultation at your {{Business_Type}} in {{City}}? If so, we would like to feature it on Tryuu, a free local guide where {{Peer_1}} and {{Peer_2}} already invite new customers.

Reply "yes" and I'll send a preview to approve. Nothing goes live without you.

{{sendingAccountFirstName}} {{sendingAccountLastName}}
Tryuu +1 (672) 380 9940

Footer for all: Tryuu Platforms Inc. | Vancouver, BC | tryuu.com. Not interested? Reply "unsubscribe" and we won't email you again.
