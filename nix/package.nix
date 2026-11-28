# Kept line for line with nixpkgs' pkgs/development/python-modules/shopify-spy/default.nix,
# so dependency changes port as a copy. Deliberate differences: src and version come
# from the flake, e2e tests run, the changelog link has no fetcher tag, and there's no
# maintainers (our pinned nixpkgs may predate lib.maintainers.ndgigliotti).
{
  lib,
  buildPythonPackage,
  src,
  version,

  # build-system
  hatchling,

  # dependencies
  defusedxml,
  httpx,
  platformdirs,
  protego,
  pydantic,
  pyyaml,
  rich,
  typer,

  # optional-dependencies
  xlsxwriter,

  # tests
  openpyxl,
  pytest-asyncio,
  pytestCheckHook,
  respx,
  writableTmpDirAsHomeHook,
}:

buildPythonPackage (finalAttrs: {
  pname = "shopify-spy";
  inherit version src;
  pyproject = true;

  build-system = [ hatchling ];

  dependencies = [
    defusedxml
    httpx
    platformdirs
    protego
    pydantic
    pyyaml
    rich
    typer
  ]
  ++ httpx.optional-dependencies.socks;

  optional-dependencies = {
    excel = [ xlsxwriter ];
  };

  nativeCheckInputs = [
    openpyxl
    pytest-asyncio
    pytestCheckHook
    respx
    writableTmpDirAsHomeHook
  ]
  ++ lib.concatAttrValues finalAttrs.passthru.optional-dependencies;

  disabledTestMarks = [
    # Calls real stores
    "integration"
    # nixpkgs also skips e2e (tight timeouts on its builders); our CI check runs them
  ];

  # Tests that start a local HTTP server fail in the Darwin sandbox without this (#85)
  __darwinAllowLocalNetworking = true;

  pythonImportsCheck = [ "shopify_spy" ];

  meta = {
    description = "Python library and CLI to scrape product data, images, and collections from Shopify, WooCommerce, Magento, BigCommerce, Squarespace and other stores";
    homepage = "https://github.com/ndgigliotti/shopify-spy";
    changelog = "https://github.com/ndgigliotti/shopify-spy/blob/v${finalAttrs.version}/CHANGELOG.md";
    license = lib.licenses.mit;
    mainProgram = "shopify-spy";
  };
})
