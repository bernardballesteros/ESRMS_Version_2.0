---
name: Apex Critical Command
colors:
  surface: '#0f131c'
  surface-dim: '#0f131c'
  surface-bright: '#353943'
  surface-container-lowest: '#0a0e17'
  surface-container-low: '#181b25'
  surface-container: '#1c1f29'
  surface-container-high: '#262a34'
  surface-container-highest: '#31353f'
  on-surface: '#dfe2ef'
  on-surface-variant: '#bac9cc'
  inverse-surface: '#dfe2ef'
  inverse-on-surface: '#2c303a'
  outline: '#849396'
  outline-variant: '#3b494c'
  surface-tint: '#00daf3'
  primary: '#c3f5ff'
  on-primary: '#00363d'
  primary-container: '#00e5ff'
  on-primary-container: '#00626e'
  inverse-primary: '#006875'
  secondary: '#7bd0ff'
  on-secondary: '#00354a'
  secondary-container: '#00a6e0'
  on-secondary-container: '#00374d'
  tertiary: '#ffe7e5'
  on-tertiary: '#68000a'
  tertiary-container: '#ffc1bc'
  on-tertiary-container: '#b1121f'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#9cf0ff'
  primary-fixed-dim: '#00daf3'
  on-primary-fixed: '#001f24'
  on-primary-fixed-variant: '#004f58'
  secondary-fixed: '#c4e7ff'
  secondary-fixed-dim: '#7bd0ff'
  on-secondary-fixed: '#001e2c'
  on-secondary-fixed-variant: '#004c69'
  tertiary-fixed: '#ffdad7'
  tertiary-fixed-dim: '#ffb3ad'
  on-tertiary-fixed: '#410004'
  on-tertiary-fixed-variant: '#930013'
  background: '#0f131c'
  on-background: '#dfe2ef'
  surface-variant: '#31353f'
typography:
  display-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 48px
    fontWeight: '700'
    lineHeight: 56px
  display-lg-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
  headline-lg-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  headline-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  headline-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
  label-lg:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
  label-sm:
    fontFamily: Inter
    fontSize: 10px
    fontWeight: '700'
    lineHeight: 14px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1.5rem
  gutter-mobile: 1rem
  margin: 2rem
  margin-mobile: 1rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2.5rem
---

## Brand & Style

This design system is engineered for high-stakes emergency dispatch and incident response environments where split-second clarity, unwavering trust, and operational precision are paramount. The target audience encompasses 911 dispatchers, emergency medical technicians, law enforcement leads, and field commanders operating under intense cognitive load.

The visual mood blends modern **tactile glassmorphism** with an authoritative **mission-critical command center aesthetic**. The experience projects unyielding stability, rigorous order, and swift intelligence. Interfaces rely on deep obsidian and navy-slate foundations paired with micro-luminescent feedback cues—delivering maximum legibility across ambient-lit command centers and high-glare vehicle-mounted terminals without inducing visual fatigue.

## Colors

The palette operates under a high-contrast dark-mode default specifically calibrated for prolonged monitoring and situational prioritization.

- **Primary (`#00E5FF` - Cyan Surge):** Represents active operational focus, real-time connectivity, verified status, and primary interactive paths. Used for active indicators, telemetry pings, and focal touch targets.
- **Secondary (`#38BDF8` - Electric Sky):** Used for non-urgent structural accents, informational highlights, selected tabs, and subtle border glimmers.
- **Tertiary (`#EF4444` - Priority Alert Red):** Strictly reserved for life-safety warnings, critical incident escalations, and unacknowledged dispatch broadcasts. Never used decoratively.
- **Neutral (`#0A0E17` - Deep Obsidian Slate):** Acts as the infinite depth canvas. Paired with surface scales at `#111827` (Command Surface) and `#1E293B` (Tactile Tile Surface) with 1px border strokes tinted in `#334155` to retain defined physical containment.

## Typography

The typographic hierarchy uses **Plus Jakarta Sans** for headlines and high-level portal labels to bring contemporary, clear geometric distinction. Body copy, telemetry counters, and operational data rely on **Inter** for its neutral, highly legible glyph architecture under dense information layouts.

Key practices:
- Numbers, call signs, and dispatch coordinate sets should leverage tabular numerals (`tnum`) for alignment stability.
- Micro-labels and dispatch code badges (`label-sm`) use all-caps with generous letter spacing (`+0.05em`) to ensure instant recognition at a glance.

## Layout & Spacing

The layout employs a responsive 12-column grid on desktop/dispatch displays, collapsing into a structured 4-column system on mobile or in-vehicle displays.

- **Desktop & Multi-Display Dispatch (1280px+):** Centered command canvas with maximum width containers (1200px) for authentication/gateway views, expanding to fluid edge-to-edge layouts with locked 24px margins for active multi-panel dispatch tracking.
- **Field & Mobile (<768px):** Dedicated single-column action stacks with guaranteed 48px touch targets, 16px lateral padding, and high touch-surface accessibility.
- **Rhythm:** Spacing follows a disciplined 8px baseline rhythm (`space-xs` through `space-xl`), ensuring vertical balance across cards, portals, and metric readouts.

## Elevation & Depth

Visual hierarchy uses a refined layered stack composed of deep obsidian surfaces, subtle glassmorphism, and colored edge illumination rather than heavy drop shadows:

- **Base Layer (Canvas):** Pure `#0A0E17` with an extremely subtle radial gradient centered at the screen focal point.
- **Surface Elevation 1 (Interactive Cards & Portal Panels):** Translucent deep navy fill (`rgba(17, 24, 39, 0.75)`) backed by a `12px` to `16px` backdrop-filter blur and framed by a crisp 1px stroke (`rgba(255, 255, 255, 0.08)`).
- **Surface Elevation 2 (Hover & Focus States):** Card elevation rises with a localized internal perimeter glow (`box-shadow: inset 0 0 0 1px rgba(0, 229, 255, 0.35)`) and an exterior cyan ambient bloom (`box-shadow: 0 12px 32px -8px rgba(0, 229, 255, 0.15)`).
- **Critical Alert Elevation:** Emergency badges and priority alert overlays incorporate high-chroma illumination (`box-shadow: 0 0 20px rgba(239, 68, 68, 0.35)`).

## Shapes

The design uses a balanced `roundedness: 2` philosophy (0.5rem base radius, 1rem for standard cards and major entrance portal elements, and 1.5rem for hero structural panels). This avoids overly playful pill forms while steering clear of cold, clinical sharp corners, projecting precision engineering and physical tactile control.

## Components

### Action Cards (Responder vs Dispatcher Gateway)
- **Structure:** Expansive, high-tactility interactive surfaces with an inset icon lockup, title, and descriptive status meta.
- **Surface:** Backlit translucent slate with a 1px border (`rgba(255, 255, 255, 0.08)`).
- **Hover/Active:** Card shifts upward by 2px, border transitions to primary cyan (`#00E5FF`), accompanied by an ambient edge bloom. Includes an assertive forward indicator chevron that increases opacity on focus.

### Buttons
- **Primary Command:** Solid cyan-to-electric-blue subtle gradient with pitch-dark slate text (`#0A0E17`), 10px 24px padding, `rounded-md` (8px), bold typography, and immediate tactical press states (`scale(0.98)`).
- **Secondary/Ghost:** Translucent glass fill (`rgba(255, 255, 255, 0.05)`), 1px solid stroke (`rgba(255, 255, 255, 0.15)`), crisp light-slate text (`#F1F5F9`), highlighting cyan on hover.

### Badges & Status Chips
- Compact, glowing status chips with a 6px pulsing dot indicator.
- **Active/Available:** Cyan background tint (`rgba(0, 229, 255, 0.12)`), cyan border, and text.
- **Critical Alert/Dispatched:** Red background tint (`rgba(239, 68, 68, 0.15)`), red border, and text.

### Input Fields
- Dark inset containers (`#0E1420`) with 1px border (`#1E293B`).
- On focus: Active 1px cyan outline with subtle zero-spread ring blur (`rgba(0, 229, 255, 0.25)`), placeholder text at 40% white opacity.

### Dispatch Quick-Action Lists & Toggles
- Row items bordered with hairline separators (`rgba(255, 255, 255, 0.05)`).
- Custom toggle switches feature an illuminated thumb when engaged, providing undeniable operational state visibility.