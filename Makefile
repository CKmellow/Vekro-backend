.PHONY: run tunnel confirm-mpesa lint format rails-selftest pesapal-register-ipn contracts-test contracts-abi anvil-deploy

run:
	.venv/bin/uvicorn app.main:app --reload

# HTTPS tunnel for Daraja callbacks (docs/demo-modes.md); host from DARAJA_CALLBACK_URL in .env.
tunnel:
	./scripts/tunnel.sh

# Fallback when no callback arrives: confirm open STK prompts with Daraja's STK query.
confirm-mpesa:
	.venv/bin/python -m app.cli confirm-mpesa-payments

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
