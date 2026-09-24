/**
 * Facts about the site that more than one page needs. The layout, the JSON-LD
 * and robots.txt read them from here, so a domain or address changes in one
 * place.
 */

/** The canonical origin. Every canonical URL, og:url and the sitemap derive from it. */
export const SITE_URL = "https://peerkola.com";

export const SITE_NAME = "Peerkola";

export const APP_URL = "https://app.peerkola.com";

export const SUPPORT_EMAIL = "support@poulse.com";

/** Used when an English page sets no description of its own. */
export const DEFAULT_DESCRIPTION =
  "Peerkola is a social feed with no ranking model. Posts travel hand to hand: each reader decides whether one carries on or stops. Posting is paid for with attention you gave first.";
