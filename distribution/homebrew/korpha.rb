# Homebrew formula for Korpha.
#
# Distributed via the Korpha tap. Users install with:
#
#   brew tap korpha/tap
#   brew install korpha
#
# Or in one shot:
#
#   brew install korpha/tap/korpha
#
# This formula goes in github.com/korpha/homebrew-tap
# (a separate repo named exactly homebrew-tap so brew picks it up).
# It is NOT auto-installed by anything in this repo — copy this file
# to that tap repo as `Formula/korpha.rb`.

class Korpha < Formula
  include Language::Python::Virtualenv

  desc "AI cofounder for solopreneurs — agents, skills, approvals, dashboard"
  homepage "https://github.com/korpha/korpha"
  url "https://github.com/korpha/korpha/archive/refs/tags/v0.1.0.tar.gz"
  # Update sha256 when tagging a release. Compute with:
  #   curl -sL https://github.com/korpha/korpha/archive/refs/tags/v0.1.0.tar.gz | sha256sum
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"
  license "MIT"
  head "https://github.com/korpha/korpha.git", branch: "main"

  depends_on "python@3.12"

  # uv installs / manages dependencies from pyproject.toml. Brew's
  # virtualenv_install_with_resources path doesn't play well with
  # uv's lock format yet, so we shell out to uv directly during install.
  depends_on "uv"

  # Optional but recommended for full functionality:
  #   - sqlite (FTS5 — usually already on macOS / Linux brew)
  depends_on "sqlite"

  def install
    # Install the package + entrypoints into a libexec venv,
    # then symlink the binary into the formula's bin/ for $PATH.
    venv_dir = libexec/"venv"
    system "uv", "venv", venv_dir.to_s, "--python", Formula["python@3.12"].opt_bin/"python3.12"
    system "uv", "pip", "install", "--python", venv_dir/"bin/python", ".", "--no-deps"
    system "uv", "pip", "install", "--python", venv_dir/"bin/python", "-r", "/dev/stdin", input: shell_output("uv export --no-emit-project --no-dev")

    bin.install_symlink venv_dir/"bin/korpha"
  end

  def caveats
    <<~EOS
      Korpha is installed. Run the interactive setup:

        korpha init      # founder + business profile
        korpha config    # add an LLM provider (OpenAI, DeepSeek, OpenRouter, etc.)
        korpha server    # start dashboard at http://localhost:8765

      Docs: https://github.com/korpha/korpha/blob/main/docs/README.md
    EOS
  end

  test do
    # Smoke test: --help works, doctor reports clean unconfigured state
    assert_match "AI cofounder", shell_output("#{bin}/korpha --help")
    ENV["KORPHA_DATA_DIR"] = testpath/"korpha-test"
    ENV["KORPHA_PROVIDERS_FILE"] = testpath/"korpha-test/nope.yaml"
    output = shell_output("#{bin}/korpha doctor")
    assert_match "Korpha health check", output
  end
end
