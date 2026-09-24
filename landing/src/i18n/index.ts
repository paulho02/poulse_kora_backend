/**
 * The site's languages. English lives at the root, every other language under
 * its own prefix (`/de/...`) - one URL per language, which is what search
 * engines index and what hreflang links pair up.
 *
 * Deliberately no redirect by Accept-Language: crawlers send none (Google
 * would only ever see English), and a reader who followed a link to one
 * language should get that language. The header's switch is the way across.
 */

export const LOCALES = ["en", "de"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

export const LOCALE_META: Record<Locale, { name: string; ogLocale: string; dateLocale: string }> = {
  en: { name: "English", ogLocale: "en_US", dateLocale: "en-GB" },
  de: { name: "Deutsch", ogLocale: "de_DE", dateLocale: "de-DE" },
};

const isLocale = (value: string | undefined): value is Locale =>
  (LOCALES as readonly string[]).includes(value ?? "");

/** The locale a site path belongs to: its first segment, or the default. */
export function localeFromPath(pathname: string): Locale {
  const first = pathname.split("/")[1];
  return isLocale(first) && first !== DEFAULT_LOCALE ? first : DEFAULT_LOCALE;
}

/** A root-relative path in `locale`: `localePath("de", "/#how")` is `/de/#how`. */
export function localePath(locale: Locale, path = "/"): string {
  return locale === DEFAULT_LOCALE ? path : `/${locale}${path}`;
}

/** The locale of a content entry, from its id (`de/impressum` is German). */
export const contentLocale = (id: string): Locale => localeFromPath(`/${id}`);

export { ui } from "./ui";
