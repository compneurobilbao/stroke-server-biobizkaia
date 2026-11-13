// Blocks reads of secret-like files (.env, credentials, keys, certificates).
// Example files such as .env.example hold no secrets and stay readable.
const SECRET_PATH_PATTERN = /(^|\/)(\.env(\..*)?|credentials\.json)$|(^|\/)(secrets?|credentials)(\/|$)|\.(pem|key|p12|pfx|crt|cer)$/i
const EXAMPLE_PATH_PATTERN = /\.example$/i

export const SecretProtection = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "read") return

      const filePath = (output?.args?.filePath || output?.args?.path || "").replace(/\/g, "/")
      if (SECRET_PATH_PATTERN.test(filePath) && !EXAMPLE_PATH_PATTERN.test(filePath)) {
        throw new Error(`Blocked secret-like file read: ${filePath}`)
      }
    },
  }
}
