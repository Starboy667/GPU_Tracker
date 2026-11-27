# Product

## Register

product

## Users

The person who runs the server and a handful of friends and family, in France. They are comfortable with
technology and buy PC hardware, TVs and video games. They are not in a hurry: they know what they want and
are waiting for the moment it is worth buying.

They open the app now and then, on a laptop or a phone, to add a product or to look at a price curve. Most
of the time they do not open it at all: the alert on their phone is the product.

Accounts are created by an administrator. There is no public sign-up. Someone who lands on the site without
an account is usually a friend who was sent the address, or who followed a shared read-only link.

## Product Purpose

Price Watch looks up products in French shops (LDLC, Materiel.net, TopAchat, Grosbill, Alternate, Joybuy,
Idealo, Dealabs, Steam, DLCompare, or any product page), learns what each one usually costs, and sends an
alert when the price really drops or the product is back in stock. Watches are private, shared with chosen
accounts, public on the server, or readable through a link.

Success: people stop checking shops by hand, trust that an alert means a real drop, and buy at a price they
are happy with.

## Brand Personality

Frank, patient, hands-on.

It says exactly what it does, with the real numbers and the real shop names. It never shouts: the whole
promise is that you can stop paying attention. Its vocabulary is the shop floor (price tags, shelf labels,
the usual price), not finance and not "AI".

The landing page is the one place allowed to show off. Everywhere else the tool gets out of the way.

## Anti-references

- Deal and coupon sites: countdowns, red banners, crossed-out "was" prices, fake urgency.
- The generic SaaS landing page: gradient blobs, glass cards, a row of three icon cards, a big-number hero.
- Trading and crypto dashboards: neon on black, candle charts, tickers.
- Decoration that does not say anything about the product.

## Design Principles

1. **Show the mechanism.** Real shops, real prices, the actual rule that triggers an alert. No abstractions
   where a concrete example fits.
2. **Calm beats urgency.** Nothing blinks, nothing counts down. A deal is stated once, clearly.
3. **The price tag is the object.** Physical retail vocabulary carries the identity, from the landing page
   to the smallest chip.
4. **One colour, one meaning.** Green is a price that dropped. Blue is what you can act on. Neither is used
   for decoration inside the app.
5. **It works without the show.** Every page is usable without JavaScript or WebGL, and with reduced motion.

## Accessibility & Inclusion

WCAG 2.2 AA. Colour is never the only signal (states always carry a label). Everything is reachable with
the keyboard, focus is always visible. `prefers-reduced-motion` turns off idle animation, parallax and
camera travel. Light and dark themes in the app follow the system setting.
