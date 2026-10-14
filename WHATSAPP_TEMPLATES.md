# WhatsApp message templates to submit to Meta

WhatsApp only lets a business send free-form messages within 24 hours of the customer's last
message. Order updates sent later (shipped, delivered...) must use an approved template.

Submit this in **WhatsApp Manager → Message templates → Create template**:

| Field | Value |
|---|---|
| Category | **Utility** |
| Name | `order_update` |
| Language | English |

**Body:**

```
Hi {{1}}, here's an update on your order #{{2}}: {{3}}. Thank you for shopping with us!
```

(Meta rejects templates that start or end with a variable, hence the closing sentence.)

Sample values Meta asks for: `{{1}}` = `Ravi`, `{{2}}` = `12`, `{{3}}` = `It has been shipped, tracking DTDC 7781234`.

Until it's approved, updates outside the 24-hour window just fail quietly (logged as
`ORDER UPDATE NOT DELIVERED`) - the order itself is still updated.

If you use a different name or language, set `WA_TEMPLATE_ORDER_UPDATE` /
`WA_TEMPLATE_ORDER_UPDATE_LANG` on the server.

---

## `seller_alert` (Utility, English) - submitted via API 2026-10-14

Alerts on the seller's own WhatsApp when a customer asks something and the seller hasn't messaged
the shop number in the last 24 hours (inside that window alerts are plain text).

```
New customer question for your shop. From: {{1}} ({{2}}). Question: {{3}}. Swipe-reply to this message to answer them.
```

Sample: `{{1}}` = `Priya`, `{{2}}` = `+91 98765 43210`, `{{3}}` = `Is the chicken masala spicy?`.
Override with `WA_TEMPLATE_SELLER_ALERT` / `WA_TEMPLATE_SELLER_ALERT_LANG`.
