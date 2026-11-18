---
approved: true           # owner approved 2026-11-18; campaigns exist as DRAFTS in Instantly (never activated by the agent)
campaigns: {HA: 4ef79cea-84b7-495d-aa10-a278521c1cba, HB: 6fea5750-2b5e-4163-8276-55272f5b428e, HC: 312e9699-ab7d-4df4-9dfd-549382e43c11}
settings: open tracking ON, link tracking OFF, first email has a 'Share it here' button ({{Signup_Link}}), follow-ups have none; senders zenbright.org x5; Tue-Thu 10-12 America/Boise = 9-11 Pacific
version: wave2-final-1
---
# Peer lines are chosen by category first, then distance (config: peers = the businesses live on Tryuu, geocoded), see pipeline notes.
# Wave 2: three new angles, 50 businesses each. No links, same expert structure as wave 1 (reply "yes" -> link sent after).
# Variables (verified data only): Business_Name, Business_Type, City, Peer_Line (same-type live Tryuu businesses),
# Reg_Quote (verbatim registration/enrolment phrase from the business's own site), Local_Peer_Line (a live Tryuu business of the same type in the same city).

## HA - registration open now (rewritten 2026-11-18: the quote now leads into the offer)
Subject: New {{Reg_Noun}} at {{Business_Name}}

Hi {{Business_Name}},

{{Reg_Line}}                      (e.g. I saw on your site: "Register Now for 2026/2027!")

{{Reg_Bridge}} That's why I'm writing: would you be open to offering a {{Free_Offer}} at your {{Business_Type}}? We'd love to have you share it on Tryuu, a local guide that helps people discover businesses by giving them a reason to show up in person. {{Peer_Line}}

It's completely free, and it only takes a minute. If you're open to it, just reply "yes" or use the button below.   [Share it here]

Per-row columns: Reg_Noun (students/clients/members), Reg_Line (quote, or a paraphrase for "spots left" widgets), Reg_Bridge (why a free first meeting makes sense: classes, limited spots, ready-to-start, counselling fit), Free_Offer (free 15-minute intro visit / intro call).
Follow-up day 3: Just following up on my note. A {{Free_Offer}} gives the people who are deciding right now an easy way to meet you. If you're open to it, reply "yes" and I'll send the details.
Follow-up day 7: Last note from me. If you'd like more people in {{City}} to find {{Business_Name}} while you are taking on new {{Reg_Noun}}, it costs nothing to be on Tryuu. Reply "yes" or "not now" and I'll leave it there.

## HB - a local peer is already on Tryuu
Subject: {{Business_Name}} on Tryuu?

Hi {{Business_Name}},

{{Local_Peer_Line}}

Would you be open to offering a free 15-minute intro visit at your {{Business_Type}} too? We help people discover local businesses by giving them a reason to show up in person, and it's completely free and only takes a minute. If you're open to it, just reply "yes" and I'll send you the details.

Follow-up day 3: Just following up on my note. We'd still love to feature a free 15-minute intro visit at {{Business_Name}} on Tryuu. Reply "yes" and I'll send the details.
Follow-up day 7: Last note from me. If you'd like more people in {{City}} to walk through your door, it costs nothing to be on Tryuu. Reply "yes" or "not now" and I'll leave it there.

## HC - written to the person who runs it (named-person mailbox)
Subject: Quick question for whoever runs {{Business_Name}}

Hi,

I'm writing to the person who runs {{Business_Name}}. Would you be open to offering a free 15-minute intro visit at your {{Business_Type}}?

We'd love to have you share it on Tryuu. We help people discover local businesses by giving them a reason to show up in person. {{Peer_Line}}

It's completely free, and it only takes a minute. If you're open to it, just reply "yes" and I'll send you the details.

Follow-up day 3: Just following up on my note. If a free 15-minute intro visit sounds good, reply "yes" and I'll send the details.
Follow-up day 7: Last note from me. Reply "yes" or "not now" and I'll leave it there.

Footer for all: Tryuu Platforms Inc. | Vancouver, BC. Not interested? Reply "unsubscribe" and we won't email you again.
