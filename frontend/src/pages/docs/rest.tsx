import React from "react";

import { BACKEND_URL } from "../../constants";

export const metadata = {
  title: "REST API - Statbotics",
};
const Page = () => {
  return <iframe src={BACKEND_URL.replace("/v3/site", "/docs")} className="w-full flex-grow" />;
};

export default Page;
