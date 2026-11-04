"use client";

import { AuroraText } from "@/components/ui/aurora-text";

import { Section } from "../section";

export function CommunitySection() {
  return (
    <Section
      title={
        <AuroraText colors={["#60A5FA", "#A5FA60", "#A560FA"]}>
          Join the Community
        </AuroraText>
      }
      subtitle="Contribute brilliant ideas to shape the future of OPIE OS. Collaborate, innovate, and make impacts."
    />
  );
}
