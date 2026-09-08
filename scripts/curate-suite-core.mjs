import { readFile, writeFile } from "node:fs/promises";

const path = new URL("../messages.json", import.meta.url);
const catalog = JSON.parse(await readFile(path, "utf8"));

catalog.taxonomy.postoochat_suite = ["", "General", "templates"];

const core = {
  START_MENU: "SUITE_ENTRY",
  ONBOARDING_ACCOUNT_EXIST: "SUITE_ACCOUNT_FOUND",
  NEW_ONBOARDING_ACCOUNT: "SUITE_ACCOUNT_NOT_FOUND",
  ONBOARDING: "SUITE_ONBOARDING_PROFILE",
  I_CONSENT: "SUITE_PRIVACY_CONSENT",
  CONSENT_REQUEST_2FA: "SUITE_2FA_SETUP",
  ONBOARD_2FA: "SUITE_2FA_VERIFY",
  SIGN_IN: "SUITE_SIGN_IN",
  CHECK_IN: "SUITE_SESSION_RESUME",
  START_HERE: "SUITE_APP_ROUTER",
  CHECK_IN_FAILED: "SUITE_ACCESS_DENIED",
};

for (const message of catalog.messages) {
  if (!core[message.name]) continue;
  message.group = "postoochat_suite";
  message.category = "General";
  // Keep the established delivery name while integrations migrate to the Suite key.
  message.suite_key = core[message.name];
}

await writeFile(path, `${JSON.stringify(catalog, null, 2)}\n`, "utf8");
