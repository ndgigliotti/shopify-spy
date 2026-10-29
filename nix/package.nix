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

  # tests
  pytest-asyncio,
  pytestCheckHook,
  respx,
}:

buildPythonPackage {
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
  ];

  nativeCheckInputs = [
    pytest-asyncio
    pytestCheckHook
    respx
  ];

  # The CLI writes its log under the platformdirs state directory
  preCheck = ''
    export HOME=$(mktemp -d)
  '';

  # Integration tests call real stores; the e2e tests only use local servers, so they stay
  disabledTestMarks = [ "integration" ];

  pythonImportsCheck = [ "shopify_spy" ];

  meta = {
    description = "Python library and CLI to scrape product data, images, and collections from Shopify and WooCommerce stores";
    homepage = "https://github.com/ndgigliotti/shopify-spy";
    changelog = "https://github.com/ndgigliotti/shopify-spy/blob/v${version}/CHANGELOG.md";
    license = lib.licenses.mit;
    mainProgram = "shopify-spy";
  };
}
