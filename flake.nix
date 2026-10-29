{
  description = "Python library and CLI to scrape product data from Shopify and WooCommerce stores";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems =
        f: lib.genAttrs systems (system: f (nixpkgs.legacyPackages.${system}.extend self.overlays.default));
    in
    {
      # Adds python3Packages.shopify-spy (for every Python) and the top-level CLI
      overlays.default = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (pyFinal: _: {
            shopify-spy = pyFinal.callPackage ./nix/package.nix {
              version = (lib.importTOML ./pyproject.toml).project.version;
              src = lib.fileset.toSource {
                root = ./.;
                fileset = lib.fileset.unions [
                  ./pyproject.toml
                  ./README.md
                  ./LICENSE
                  ./shopify_spy
                  ./scripts
                  ./tests
                ];
              };
            };
          })
        ];
        shopify-spy = final.python3Packages.toPythonApplication final.python3Packages.shopify-spy;
      };

      packages = forAllSystems (pkgs: {
        default = pkgs.shopify-spy;
        shopify-spy = pkgs.shopify-spy;
        python = pkgs.python3.withPackages (ps: [ ps.shopify-spy ]);
      });

      checks = forAllSystems (pkgs: {
        inherit (pkgs) shopify-spy;
      });

      devShells = forAllSystems (pkgs: {
        # Python with shopify_spy importable, for library scripts
        default = pkgs.mkShell {
          packages = [ self.packages.${pkgs.stdenv.hostPlatform.system}.python ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
