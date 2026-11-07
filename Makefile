.PHONY: run lint format rails-selftest pesapal-register-ipn contracts-test contracts-abi anvil-deploy

run:
	.venv/bin/uvicorn app.main:app --reload

lint:
	.venv/bin/ruff check .
	.venv/bin/black --check .

format:
	.venv/bin/ruff check . --fix
	.venv/bin/black .

rails-selftest:
	.venv/bin/python -m app.services.custody.loop_selftest

pesapal-register-ipn:
	.venv/bin/python -m app.services.custody.pesapal_ipn_registration

contracts-test:
	cd contracts && forge fmt --check && forge test

contracts-abi:
	cd contracts && forge build && jq '.abi' out/AnchorRegistry.sol/AnchorRegistry.json > abi/AnchorRegistry.json

# Deploys to a local Anvil (anvil --port 8545) with its first well-known development key.
anvil-deploy:
	cd contracts && forge script script/Deploy.s.sol --rpc-url anvil --broadcast \
		--private-key $$(cast wallet private-key --mnemonic "test test test test test test test test test test test junk")
