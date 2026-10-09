import { test, describe } from "node:test";
import assert from "node:assert/strict";
import {
  estimateTokens,
  extractTerms,
  keywordScore,
  keywordRerank,
  isDocumentationQuery,
  synonymSubstitutionVariant,
  reorderingVariants,
  genericParaphraseVariants,
  expandQueries,
  groupByFile,
  matchesWhenKeywords,
} from "../../src/context_assembler";
import type { QdrantHit } from "../../src/qdrant";
import type { ProfileInfo } from "../../src/config";

function hit(relativePath: string, score: number, text = "", startLine = 1): QdrantHit {
  return {
    score,
    payload: {
      project_key: "test",
      relative_path: relativePath,
      language: "typescript",
      text,
      start_line: startLine,
      end_line: startLine,
      symbol: null,
      chunk_type: "function",
    },
  };
}

function profile(components: Partial<ProfileInfo["components"][number]>[]): ProfileInfo {
  return {
    monorepo: components.length > 1,
    components: components.map((c) => ({
      language: "unknown", layer: "backend", framework: null, build_system: null, ...c,
    })),
  };
}

describe("estimateTokens", () => {
  test("estimates ~1 token per 4 characters, rounded up", () => {
    assert.equal(estimateTokens(""), 0);
    assert.equal(estimateTokens("abcd"), 1);
    assert.equal(estimateTokens("abcde"), 2);
    assert.equal(estimateTokens("a".repeat(400)), 100);
  });
});

describe("extractTerms", () => {
  test("lowercases, dedupes, and drops short tokens", () => {
    const terms = extractTerms("The Cat sat on it");
    assert.ok(terms.includes("the"));
    assert.ok(terms.includes("cat"));
    assert.ok(terms.includes("sat"));
    assert.ok(!terms.includes("on")); // length 2, filtered out
    assert.ok(!terms.includes("it")); // length 2, filtered out
  });

  test("splits camelCase identifiers into sub-terms", () => {
    const terms = extractTerms("UserService");
    assert.ok(terms.includes("userservice"));
    assert.ok(terms.includes("user"));
    assert.ok(terms.includes("service"));
  });
});

describe("keywordScore", () => {
  test("returns 0 for no query terms", () => {
    assert.equal(keywordScore([], "some text"), 0);
  });

  test("counts term occurrences, capped at 1.0", () => {
    assert.equal(keywordScore(["foo"], "foo bar foo"), 0.2); // 2 hits / 10
    assert.equal(keywordScore(["foo"], "foo ".repeat(20)), 1.0); // capped
  });
});

describe("keywordRerank", () => {
  test("no query terms: hits unchanged, why is plain vector-similarity note", () => {
    const hits = [hit("a.ts", 0.9), hit("b.ts", 0.8)];
    const result = keywordRerank("it on", hits); // both terms filtered (length < 3)
    assert.equal(result.length, 2);
    assert.match(result[0].why!, /vector similarity 0\.90/);
  });

  test("boosts and re-sorts hits containing query keywords", () => {
    const hits = [
      hit("a.ts", 0.5, "no keywords here"),
      hit("b.ts", 0.45, "user authentication logic"),
    ];
    const result = keywordRerank("user authentication", hits);
    // a: 0.75*0.5 + 0.25*0    = 0.375
    // b: 0.75*0.45 + 0.25*0.2 = 0.3875  -> b overtakes a despite the lower base score.
    assert.equal(result[0].payload.relative_path, "b.ts");
    assert.match(result[0].why!, /keyword boost/);
  });
});

describe("isDocumentationQuery", () => {
  test("matches known documentation-intent keywords", () => {
    assert.equal(isDocumentationQuery("how does auth work?"), true);
    assert.equal(isDocumentationQuery("explain the architecture"), true);
    assert.equal(isDocumentationQuery("UserService.findById"), false);
  });
});

describe("synonymSubstitutionVariant", () => {
  test("substitutes a generic verb with its synonym group partner", () => {
    assert.equal(synonymSubstitutionVariant("how do we decide the winner"), "how do we determine the winner");
  });

  test("returns null when no synonym-group verb is present", () => {
    assert.equal(synonymSubstitutionVariant("UserService.findById"), null);
  });

  test("is case-insensitive but preserves surrounding text", () => {
    const result = synonymSubstitutionVariant("Configure the client");
    assert.equal(result, "set up the client");
  });
});

describe("reorderingVariants", () => {
  test("reorders 'how does X verb Y' into two paraphrase variants", () => {
    const variants = reorderingVariants("how does the service persist orders");
    assert.deepEqual(variants, [
      "orders the service",
      "the service orders implementation",
    ]);
  });

  test("known quirk: a noun containing a verb-synonym as a substring can match early", () => {
    // "router" contains "route" (one of the GENERIC_VERB_SYNONYM_GROUPS verbs),
    // and the verb pattern is `(?:verb)\w*` (to allow conjugations like
    // "routes") — so "router" itself satisfies the verb slot before reaching
    // the intended "dispatch" verb later in the query. Documented here as
    // discovered behavior, not a hypothetical: x ends up as "the" (not "the
    // router"), and "dispatch requests" is treated as the y-clause.
    const variants = reorderingVariants("how does the router dispatch requests");
    assert.deepEqual(variants, [
      "dispatch requests the",
      "the dispatch requests implementation",
    ]);
  });

  test("returns empty array when the query doesn't match the pattern", () => {
    assert.deepEqual(reorderingVariants("UserService.findById"), []);
  });
});

describe("genericParaphraseVariants", () => {
  test("combines synonym substitution and reordering variants", () => {
    const variants = genericParaphraseVariants("how does the router dispatch requests");
    // "dispatch" triggers both a synonym substitution ("route") AND the
    // reordering pattern (since "dispatch" is itself one of the verbs).
    assert.ok(variants.length >= 1);
  });

  test("returns empty array for a query with neither pattern", () => {
    assert.deepEqual(genericParaphraseVariants("UserService.findById"), []);
  });
});

describe("expandQueries", () => {
  test("plain query with no profile components returns just the original query", () => {
    const result = expandQueries("UserService.findById", profile([]));
    assert.deepEqual(result, ["UserService.findById"]);
  });

  test("Kotlin backend expands with Koin DI terms", () => {
    const result = expandQueries("dependency injection setup", profile([{ language: "kotlin" }]));
    assert.ok(result.some((q) => q.includes("Koin module dependency injection")));
  });

  test("Kotlin + REST-shaped query also expands with Ktor routing", () => {
    const result = expandQueries("GET endpoint for users", profile([{ language: "kotlin" }]));
    assert.ok(result.some((q) => q.includes("Ktor routing route")));
  });

  test("non-frontend, non-Kotlin/Java repo does not get framework-specific expansions", () => {
    // Regression coverage for the fixed false-positive bug (see "Profile
    // auto-detection" in docs/configuration.md):
    // a plain Python backend component must not trigger React/Angular/Vue/Koin/Spring
    // expansions just because of loose string matching on an old flat profile string.
    const result = expandQueries("how is data persisted", profile([{ language: "python", layer: "backend" }]));
    assert.deepEqual(result, ["how is data persisted"]);
  });

  test("caps at MAX_QUERY_VARIANTS (4) even when multiple expansions would apply", () => {
    const result = expandQueries(
      "GET endpoint persist repository entity jpa",
      profile([{ language: "java" }]),
    );
    assert.ok(result.length <= 4);
  });

  test("frontend layer without a named framework still gets generic frontend expansions", () => {
    const result = expandQueries("component view for users", profile([{ language: "typescript", layer: "frontend" }]));
    assert.ok(result.some((q) => q.includes("React component hooks")));
  });
});

describe("groupByFile", () => {
  test("groups hits by relative_path and sorts groups by max score descending", () => {
    const hits = [
      hit("a.ts", 0.3),
      hit("b.ts", 0.9),
      hit("a.ts", 0.7),
    ];
    const groups = groupByFile(hits);
    assert.equal(groups.length, 2);
    assert.equal(groups[0].relativePath, "b.ts");
    assert.equal(groups[1].relativePath, "a.ts");
    assert.equal(groups[1].maxScore, 0.7); // max of the two a.ts hits
    assert.equal(groups[1].topChunks.length, 2);
  });

  test("empty input returns empty array", () => {
    assert.deepEqual(groupByFile([]), []);
  });
});

describe("matchesWhenKeywords", () => {
  test("matches a comma-separated keyword list case-insensitively", () => {
    assert.equal(matchesWhenKeywords("What is the User DTO shape?", "user, contract"), true);
  });

  test("does not match on partial word overlap (word-boundary anchored)", () => {
    assert.equal(matchesWhenKeywords("username field", "user"), false);
  });

  test("empty `when` string never matches", () => {
    assert.equal(matchesWhenKeywords("anything", ""), false);
  });
});
