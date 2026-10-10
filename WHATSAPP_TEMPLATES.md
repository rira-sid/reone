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
