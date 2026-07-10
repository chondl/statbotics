/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: false,
  images: {
    domains: [
      // imgur
      "i.imgur.com",
      // instagram, through TBA
      "www.thebluealliance.com",
    ],
  },
  env: {
    PROD: process.env.PROD || "false",
    // Staging overrides (inlined at build time). When unset, constants.tsx
    // falls back to the PROD/local defaults.
    BACKEND_URL: process.env.BACKEND_URL || "",
    BUCKET_URL: process.env.BUCKET_URL || "",
  },
};

module.exports = nextConfig;
