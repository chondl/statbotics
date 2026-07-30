import React from "react";

import TeamLineChart from "../../components/Figures/TeamLine";
import { APITeamMatch, APITeamYear } from "../../types/api";

const FigureSection = ({
  teamNum,
  year,
  teamYear,
  matches,
}: {
  teamNum: number;
  year: number;
  teamYear: APITeamYear;
  matches: APITeamMatch[];
}) => {
  // Offseason matches carry per-event sandbox EPA, which is not on the same
  // scale as the season line -- plotting it would imply a rating change that
  // never happened.
  const seasonMatches = matches.filter((match) => !match.offseason);

  return (
    <div className="w-full h-auto flex flex-col justify-center items-center px-2">
      <div className="w-full text-2xl font-bold mb-4">EPA Over Time</div>
      <TeamLineChart teamNum={teamNum} year={year} teamYear={teamYear} data={seasonMatches} />
    </div>
  );
};

export default FigureSection;
