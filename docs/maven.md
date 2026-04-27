# Maven

## Prerequisites

To use Hermeto with [Maven][maven] locally, ensure you have Maven (3.8+) and
Java (17+) installed. Then, generate a lockfile for your project using the
[maven-lockfile][maven-lockfile] plugin:

```bash
mvn io.github.chains-project:maven-lockfile:generate
```

This produces a `lockfile.json` in your project root containing all resolved
dependencies with their download URLs and checksums. Commit this file to your
repository.

For multi-module projects, run the command from the root — it generates a
lockfile per module.

!!! note

    If your project's dependency tree changes (e.g., you update `pom.xml`),
    regenerate the lockfile. You can add a CI check with
    `mvn io.github.chains-project:maven-lockfile:validate` to catch stale
    lockfiles before they reach the build pipeline.

## Usage

Run the following command to prefetch your project's dependencies:

```bash
hermeto fetch-deps \
  --source ./my-project \
  --output ./hermeto-output \
  '{"type": "x-maven", "path": "."}'
```

For multi-module projects, specify each module:

```bash
hermeto fetch-deps \
  --source ./my-project \
  --output ./hermeto-output \
  '[{"type": "x-maven", "path": "module-a"}, {"type": "x-maven", "path": "module-b"}]'
```

Hermeto downloads all artifacts (JARs, POMs, checksums) into a local Maven
repository layout under `hermeto-output/deps/maven/`. It also generates a
`settings.xml` that configures Maven to use this local mirror.

After prefetching, inject the configuration files into your project:

```bash
hermeto inject-files --for-output-dir /tmp/hermeto-output hermeto-output
```

## Hermetic build

After prefetching and injecting files, build your project offline. Here is an
example Dockerfile:

```dockerfile
FROM registry.access.redhat.com/ubi9/openjdk-21:latest

WORKDIR /app

COPY . .

RUN mvn package -o ${MAVEN_ARGS}
```

Build with the prefetched dependencies mounted and network disabled:

```bash
podman build . \
  --volume "$(realpath ./hermeto-output)":/tmp/hermeto-output:Z \
  --network none \
  --tag my-java-app
```

Maven resolves all dependencies from the local mirror — no network access
required.

## Output structure

After `fetch-deps`, the output directory contains:

```
hermeto-output/
├── bom.json               # CycloneDX SBOM
├── settings.xml           # Maven settings (local mirror)
└── deps/
    └── maven/             # Standard Maven repository layout
        └── org/example/foo/1.0.0/
            ├── foo-1.0.0.jar
            ├── foo-1.0.0.jar.sha256
            ├── foo-1.0.0.pom
            ├── foo-1.0.0.pom.sha256
            └── _remote.repositories
```

Two environment variables are set for the build phase:

| Variable | Value |
|----------|-------|
| `MAVEN_OPTS` | `-Dmaven.repo.local=${output_dir}/deps/maven` |
| `MAVEN_ARGS` | `-s ${output_dir}/settings.xml` |

## Multi-module projects

In multi-module Maven projects, sibling modules (reactor dependencies) appear
in the lockfile with an empty `resolved` URL since they are built from source,
not downloaded. Hermeto skips these automatically and logs them at INFO level.
Their transitive external dependencies are still prefetched.

Shared dependencies across modules are deduplicated — each artifact is
downloaded only once regardless of how many modules declare it.

## Limitations

- **Experimental status**: The Maven backend uses the `x-maven` type prefix,
  indicating it is not yet stabilized. The interface may change.
- **SNAPSHOT versions**: Not supported. SNAPSHOT artifacts have
  non-deterministic download URLs.
- **POM integrity**: POM file checksums are server-attested only. The lockfile
  does not declare checksums for POM files.

## Open questions

The following items are not yet addressed and may require upstream discussion.

- **`pom.lockfile.xml` integration**: The maven-lockfile plugin's `freeze` goal
  can generate a `pom.lockfile.xml` that pins all dependency versions. The
  Konflux feature refinement (KONFLUX-3775) references building with this
  locked POM (`-f pom.lockfile.xml`). Currently, Hermeto does not generate this
  argument. It is unclear whether this is a Hermeto responsibility (add to
  `MAVEN_ARGS`) or a build pipeline concern (Tekton task configuration). The
  `freeze` goal serves a different purpose (version pinning for
  reproducibility) than the `generate` goal (checksum lockfile for integrity
  verification).
- **Explicit offline mode**: The Konflux feature refinement specifies running
  Maven in offline mode. The generated `settings.xml` effectively prevents
  network access by mirroring all repositories to the local prefetch directory.
  Adding an explicit `-o` flag to `MAVEN_ARGS` would provide defense-in-depth
  but may cause failures if any plugin attempts network access for
  non-dependency purposes (e.g., site generation, deployment).

[maven]: https://maven.apache.org/
[maven-lockfile]: https://github.com/chains-project/maven-lockfile
