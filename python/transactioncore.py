# LICENSE: MIT // github.com/John0n1/ON1Builder

import asyncio
import aiohttp
import hexbytes
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound, ContractLogicError
from eth_account import Account
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from decimal import Decimal

from abiregistry import ABIRegistry
from apiconfig import APIConfig
from configuration import Configuration
from marketmonitor import MarketMonitor
from mempoolmonitor import MempoolMonitor  
from noncecore import NonceCore
from safetynet import SafetyNet
from loggingconfig import setup_logging
import logging

logger = setup_logging("TransactionCore", level=logging.INFO)


class TransactionCore:
    """
    Main transaction engine responsible for building, simulating, signing, and executing transactions.
    
    This class supports various strategies including front-run, back-run, sandwich attacks, and flashloan transactions.
    It interacts with smart contracts, estimates gas costs, and handles error logging.
    """

    DEFAULT_GAS_LIMIT: int = 100_000
    DEFAULT_PROFIT_TRANSFER_MULTIPLIER: int = 10**18
    DEFAULT_GAS_PRICE_GWEI: int = 50

    def __init__(
        self,
        web3: AsyncWeb3,
        account: Account,
        configuration: Optional[Configuration] = None,
        apiconfig: Optional[APIConfig] = None,
        marketmonitor: Optional[MarketMonitor] = None,
        mempoolmonitor: Optional[MempoolMonitor] = None,  # Corrected type annotation
        noncecore: Optional[NonceCore] = None,
        safetynet: Optional[SafetyNet] = None,
        gas_price_multiplier: float = 1.1,
        erc20_abi: Optional[List[Dict[str, Any]]] = None,
        uniswap_address: Optional[str] = None,
        uniswap_abi: Optional[List[Dict[str, Any]]] = None
    ):
        """
        Initialize TransactionCore with required components.
        """
        self.web3: AsyncWeb3 = web3
        self.account: Account = account
        self.configuration: Optional[Configuration] = configuration
        self.marketmonitor: Optional[MarketMonitor] = marketmonitor
        self.mempoolmonitor: Optional[MempoolMonitor] = mempoolmonitor
        self.apiconfig: Optional[APIConfig] = apiconfig
        self.noncecore: Optional[NonceCore] = noncecore
        self.safetynet: Optional[SafetyNet] = safetynet
        self.gas_price_multiplier = gas_price_multiplier
        self.erc20_abi: List[Dict[str, Any]] = erc20_abi or []
        self.current_profit: Decimal = Decimal("0")
        self.abiregistry: ABIRegistry = ABIRegistry()
        self.uniswap_address: str = uniswap_address
        self.uniswap_abi: List[Dict[str, Any]] = uniswap_abi or []
        self.uniswap_router_contract = None 
        self.sushiswap_router_contract = None

    def normalize_address(self, address: str) -> str:
        """
        Normalize an Ethereum address to checksum format.
        """
        try:
            return self.web3.to_checksum_address(address)
        except Exception as e:
            self.handle_error(e, "normalize_address", {"address": address})
            raise

    async def initialize(self) -> None:
        """
        Initialize the TransactionCore by loading ABIs, initializing contracts,
        and performing basic contract validations.
        """
        try:
            abiregistry = ABIRegistry()
            await abiregistry.initialize(self.configuration.BASE_PATH)

            # Load required ABIs from the registry
            aave_flashloan_abi = abiregistry.get_abi('aave_flashloan')
            aave_pool_abi = abiregistry.get_abi('aave')
            uniswap_abi = abiregistry.get_abi('uniswap')
            sushiswap_abi = abiregistry.get_abi('sushiswap')

            # Initialize and validate Aave flashloan contract
            self.aave_flashloan = self.web3.eth.contract(
                address=self.normalize_address(self.configuration.AAVE_FLASHLOAN_ADDRESS),
                abi=aave_flashloan_abi
            )
            await self._validate_contract(self.aave_flashloan, "Aave Flashloan", 'aave_flashloan')

            # Initialize and validate Aave pool contract
            self.aave_pool = self.web3.eth.contract(
                address=self.normalize_address(self.configuration.AAVE_POOL_ADDRESS),
                abi=aave_pool_abi
            )
            await self._validate_contract(self.aave_pool, "Aave Lending Pool", 'aave')

            # Initialize and validate Uniswap router contract if available
            if uniswap_abi and self.configuration.UNISWAP_ADDRESS:
                self.uniswap_router_contract = self.web3.eth.contract(
                    address=self.normalize_address(self.configuration.UNISWAP_ADDRESS),
                    abi=uniswap_abi
                )
                await self._validate_contract(self.uniswap_router_contract, "Uniswap Router", 'uniswap')
            else:
                logger.warning("Uniswap ABI or address not configured. Uniswap strategies will be unavailable.")

            # Initialize and validate Sushiswap router contract if available
            if sushiswap_abi and self.configuration.SUSHISWAP_ADDRESS:
                self.sushiswap_router_contract = self.web3.eth.contract(
                    address=self.normalize_address(self.configuration.SUSHISWAP_ADDRESS),
                    abi=sushiswap_abi
                )
                await self._validate_contract(self.sushiswap_router_contract, "Sushiswap Router", 'sushiswap')
            else:
                logger.warning("Sushiswap ABI or address not configured. Sushiswap strategies will be unavailable.")

            logger.debug("TransactionCore initialized successfully")
        except Exception as e:
            self.handle_error(e, "initialize")
            raise

    async def _validate_contract(self, contract: Any, name: str, abi_type: str) -> None:
        """
        Validate a contract by trying a set of known methods or checking for code existence.
        """
        try:
            validation_methods = {
                'Lending Pool': [
                    ('implementation', []),
                    ('admin', []),
                    ('getReservesList', []),
                    ('getReserveData', ['0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2']),
                    ('ADDRESSES_PROVIDER', [])
                ],
                'Flashloan': [
                    ('ADDRESSES_PROVIDER', []),
                    ('owner', []),
                    ('POOL', []),
                    ('FLASHLOAN_PREMIUM_TOTAL', [])
                ],
                'Router': [
                    ('factory', []),
                    ('WETH', [])
                ]
            }

            methods_to_try = []
            if 'Lending Pool' in name:
                methods_to_try = validation_methods['Lending Pool']
            elif 'Flashloan' in name:
                methods_to_try = validation_methods['Flashloan']
            elif abi_type in ['uniswap', 'sushiswap']:
                methods_to_try = validation_methods['Router']

            for method_name, args in methods_to_try:
                try:
                    if hasattr(contract.functions, method_name):
                        await getattr(contract.functions, method_name)(*args).call()
                        logger.debug(f"{name} validated via {method_name}()")
                        return
                except (ContractLogicError, OverflowError) as e:
                    logger.debug(f"Method {method_name} failed: {str(e)}")
                    continue

            code = await self.web3.eth.get_code(contract.address)
            if code and code != '0x':
                logger.debug(f"{name} validated via code existence")
                return

            raise ValueError(f"No valid validation method found for {name}")

        except Exception as e:
            self.handle_error(e, "_validate_contract", {"contract": contract, "name": name, "abi_type": abi_type})
            logger.warning(f"Contract validation warning for {name}: {e}")
            raise

    async def build_transaction(self, function_call: Any, additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Build a transaction with dynamic gas parameters and EIP-1559 support if available.
        """
        additional_params = additional_params or {}
        try:
            chain_id = await self.web3.eth.chain_id
            latest_block = await self.web3.eth.get_block('latest')
            supports_eip1559 = 'baseFeePerGas' in latest_block

            tx_params = {
                'chainId': chain_id,
                'nonce': await self.noncecore.get_nonce(),
                'from': self.account.address,
            }

            if supports_eip1559:
                base_fee = latest_block['baseFeePerGas']
                priority_fee = await self.web3.eth.max_priority_fee
                tx_params.update({
                    'maxFeePerGas': int(base_fee * 2),
                    'maxPriorityFeePerGas': int(priority_fee)
                })
            else:
                tx_params.update(await self._get_dynamic_gas_parameters())

            tx_details = function_call.buildTransaction(tx_params)
            tx_details.update(additional_params)
            estimated_gas = await self.estimate_gas_smart(tx_details)
            tx_details['gas'] = int(estimated_gas * 1.1)
            return tx_details

        except Exception as e:
            self.handle_error(e, "build_transaction", {"function_call": function_call, "additional_params": additional_params})
            raise

    async def _get_dynamic_gas_parameters(self) -> Dict[str, int]:
        """
        Retrieve dynamic gas parameters adjusted by the configured multiplier.
        """
        try:
            gas_price_gwei = await self.safetynet.get_dynamic_gas_price()
            logger.debug(f"Fetched gas price: {gas_price_gwei} Gwei")
        except Exception as e:
            self.handle_error(e, "_get_dynamic_gas_parameters")
            gas_price_gwei = Decimal(self.DEFAULT_GAS_PRICE_GWEI)
        gas_price = int(self.web3.to_wei(gas_price_gwei * self.gas_price_multiplier, "gwei"))
        return {"gasPrice": gas_price}

    async def estimate_gas_smart(self, tx: Dict[str, Any]) -> int:
        """
        Estimate the gas required for a transaction with a fallback to a default value.
        """
        try:
            gas_estimate = await self.web3.eth.estimate_gas(tx)
            logger.debug(f"Estimated gas: {gas_estimate}")
            return gas_estimate
        except (ContractLogicError, TransactionNotFound) as e:
            self.handle_error(e, "estimate_gas_smart", {"tx": tx})
            return self.DEFAULT_GAS_LIMIT
        except Exception as e:
            self.handle_error(e, "estimate_gas_smart", {"tx": tx})
            return self.DEFAULT_GAS_LIMIT

    async def execute_transaction(self, tx: Dict[str, Any]) -> Optional[str]:
        """
        Execute a transaction with configurable retries and gas price cap checks.
        """
        max_retries = self.configuration.MEMPOOL_MAX_RETRIES
        retry_delay = self.configuration.MEMPOOL_RETRY_DELAY

        for attempt in range(1, max_retries + 1):
            try:
                signed_tx = await self.sign_transaction(tx)
                tx_hash = await self.call_contract_function(signed_tx)
                logger.debug(f"Transaction sent successfully: {tx_hash.hex()}")
                return tx_hash.hex()
            except (TransactionNotFound, ContractLogicError) as e:
                self.handle_error(e, "execute_transaction", {"tx": tx})
            except Exception as e:
                self.handle_error(e, "execute_transaction", {"tx": tx})
                await asyncio.sleep(retry_delay * (attempt + 1))

            gas_price_gwei = Decimal(tx.get("gasPrice", self.DEFAULT_GAS_PRICE_GWEI))
            if gas_price_gwei > self.configuration.MAX_GAS_PRICE_GWEI:
                logger.warning(f"Gas price {gas_price_gwei} Gwei exceeds maximum threshold of {self.configuration.MAX_GAS_PRICE_GWEI} Gwei.")
                return None

        logger.error("Failed to execute transaction after retries")
        return None

    async def sign_transaction(self, transaction: Dict[str, Any]) -> bytes:
        """
        Sign a transaction using the account's private key.
        """
        try:
            signed_tx = self.web3.eth.account.sign_transaction(
                transaction,
                private_key=self.account.key,
            )
            logger.debug(f"Transaction signed successfully: Nonce {transaction['nonce']}")
            return signed_tx.rawTransaction
        except KeyError as e:
            self.handle_error(e, "sign_transaction", {"transaction": transaction})
            raise
        except Exception as e:
            self.handle_error(e, "sign_transaction", {"transaction": transaction})
            raise

    async def handle_eth_transaction(self, target_tx: Dict[str, Any]) -> bool:
        """
        Process an ETH transfer transaction by building and executing it.
        """
        tx_hash = target_tx.get("tx_hash", "Unknown")
        logger.debug(f"Handling ETH transaction {tx_hash}")
        try:
            eth_value = target_tx.get("value", 0)
            if eth_value <= 0:
                logger.debug("Transaction value is zero or negative. Skipping.")
                return False

            tx_details = {
                "to": target_tx.get("to", ""),
                "value": eth_value,
                "gas": 21_000,
                "nonce": await self.noncecore.get_nonce(),
                "chainId": self.web3.eth.chain_id,
                "from": self.account.address,
            }
            original_gas_price = int(target_tx.get("gasPrice", 0))
            if original_gas_price <= 0:
                logger.warning("Original gas price is zero or negative. Skipping.")
                return False
            tx_details["gasPrice"] = int(original_gas_price * self.configuration.ETH_TX_GAS_PRICE_MULTIPLIER)

            eth_value_ether = self.web3.from_wei(eth_value, "ether")
            logger.debug(f"Building ETH front-run transaction for {eth_value_ether} ETH to {tx_details['to']}")
            tx_hash_executed = await self.execute_transaction(tx_details)
            if tx_hash_executed:
                logger.debug(f"Successfully executed ETH transaction with hash: {tx_hash_executed}")
                return True
            else:
                logger.warning("Failed to execute ETH transaction. Retrying...")
        except Exception as e:
            self.handle_error(e, "handle_eth_transaction", {"target_tx": target_tx})
        return False

    def calculate_flashloan_amount(self, target_tx: Dict[str, Any]) -> int:
        """
        Calculate the flashloan amount based on the estimated profit and configured percentage.
        """
        estimated_profit = target_tx.get("profit", 0)
        if estimated_profit > 0:
            flashloan_percentage = self.configuration.FLASHLOAN_BACK_RUN_PROFIT_PERCENTAGE
            flashloan_amount = int(
                Decimal(estimated_profit) * Decimal(str(flashloan_percentage)) * Decimal("1e18")
            )
            logger.debug(f"Calculated flashloan amount: {flashloan_amount} Wei based on estimated profit.")
            return flashloan_amount
        else:
            logger.debug("No estimated profit. Setting flashloan amount to 0.")
            return 0

    async def simulate_transaction(self, transaction: Dict[str, Any]) -> bool:
        """
        Simulate a transaction to verify its potential success without executing it.
        """
        logger.debug(f"Simulating transaction with nonce {transaction.get('nonce', 'Unknown')}.")
        try:
            await self.web3.eth.call(transaction, block_identifier="pending")
            logger.debug("Transaction simulation succeeded.")
            return True
        except ContractLogicError as e:
            self.handle_error(e, "simulate_transaction", {"transaction": transaction})
            return False
        except Exception as e:
            self.handle_error(e, "simulate_transaction", {"transaction": transaction})
            return False

    async def prepare_flashloan_transaction(
        self, flashloan_asset: str, flashloan_amount: int
    ) -> Optional[Dict[str, Any]]:
        """
        Prepare a flashloan transaction for a given asset and amount.
        """
        if flashloan_amount <= 0:
            logger.debug("Flashloan amount is 0 or less, skipping flashloan transaction preparation.")
            return None
        try:
            function_call = self.aave_flashloan.functions.fn_RequestFlashLoan(
                flashloan_asset,
                flashloan_amount
            )
            tx = await self.build_transaction(function_call)
            return tx
        except ContractLogicError as e:
            self.handle_error(e, "prepare_flashloan_transaction", {"flashloan_asset": flashloan_asset, "flashloan_amount": flashloan_amount})
            return None
        except Exception as e:
            self.handle_error(e, "prepare_flashloan_transaction", {"flashloan_asset": flashloan_asset, "flashloan_amount": flashloan_amount})
            return None

    async def send_bundle(self, transactions: List[Dict[str, Any]]) -> bool:
        """
        Send a bundle of transactions to MEV relays with retries based on configuration.
        """
        try:
            signed_txs = [await self.sign_transaction(tx) for tx in transactions]
            bundle_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendBundle",
                "params": [
                    {
                        "txs": [signed_tx.hex() for signed_tx in signed_txs],
                        "blockNumber": hex(await self.web3.eth.block_number + 1),
                    }
                ],
            }

            mev_builders = self.configuration.MEV_BUILDERS
            max_retries = self.configuration.MEMPOOL_MAX_RETRIES
            retry_delay = self.configuration.MEMPOOL_RETRY_DELAY
            successes = []

            for builder in mev_builders:
                headers = {
                    "Content-Type": "application/json",
                    builder["auth_header"]: f"{self.account.address}:{self.account.key}",
                }
                for attempt in range(1, max_retries + 1):
                    try:
                        logger.debug(f"Attempt {attempt} to send bundle via {builder['name']}.")
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                builder["url"],
                                json=bundle_payload,
                                headers=headers,
                                timeout=30,
                            ) as response:
                                response.raise_for_status()
                                response_data = await response.json()

                                if "error" in response_data:
                                    self.handle_error(ValueError(response_data["error"]), "send_bundle", {"transactions": transactions})
                                    raise ValueError(response_data["error"])

                                logger.info(f"Bundle sent successfully via {builder['name']}.")
                                successes.append(builder['name'])
                                break
                    except aiohttp.ClientResponseError as e:
                        self.handle_error(e, "send_bundle", {"transactions": transactions})
                        if attempt < max_retries:
                            await asyncio.sleep(retry_delay * attempt)
                    except ValueError as e:
                        self.handle_error(e, "send_bundle", {"transactions": transactions})
                        break
                    except Exception as e:
                        self.handle_error(e, "send_bundle", {"transactions": transactions})
                        if attempt < max_retries:
                            await asyncio.sleep(retry_delay * attempt)

            if successes:
                await self.noncecore.refresh_nonce()
                logger.info(f"Bundle successfully sent to builders: {', '.join(successes)}")
                return True
            else:
                logger.warning("Failed to send bundle to any MEV builders.")
                return False

        except Exception as e:
            self.handle_error(e, "send_bundle", {"transactions": transactions})
            return False

    async def _validate_transaction(self, tx: Dict[str, Any], operation: str, min_value: float = 0.0) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Validate a transaction's structure and decode its input.
        """
        if not isinstance(tx, dict):
            logger.debug("Invalid transaction format provided!")
            return False, None, None

        required_fields = ["input", "to", "value", "gasPrice"]
        if not all(field in tx for field in required_fields):
            missing = [field for field in required_fields if field not in tx]
            logger.debug(f"Missing required parameters for {operation}: {missing}")
            return False, None, None

        try:
            decoded_tx = await self.decode_transaction_input(
                tx.get("input", "0x"),
                self.web3.to_checksum_address(tx.get("to", ""))
            )
            if not decoded_tx or "params" not in decoded_tx:
                logger.debug(f"Failed to decode transaction input for {operation}")
                return False, None, None

            path = decoded_tx["params"].get("path", [])
            if not path or not isinstance(path, list) or len(path) < 2:
                logger.debug(f"Invalid path parameter for {operation}")
                return False, None, None

            token_symbol = await self.apiconfig.get_token_symbol(path[0])
            if not token_symbol:
                logger.debug("Could not determine token symbol")
                return False, None, None

            if float(tx.get("value", 0)) < min_value:
                logger.debug(f"Transaction value below minimum threshold of {min_value}")
                return False, None, None

            return True, decoded_tx, token_symbol

        except Exception as e:
            self.handle_error(e, "_validate_transaction", {"tx": tx, "operation": operation})
            return False, None, None

    async def front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a front-run strategy on the target transaction.
        """
        valid, decoded_tx, _ = await self._validate_transaction(target_tx, "front-run")
        if not valid:
            return False

        try:
            path = decoded_tx["params"]["path"]
            flashloan_tx = await self._prepare_flashloan(path[0], target_tx)
            front_run_tx = await self._prepare_front_run_transaction(target_tx)

            if not all([flashloan_tx, front_run_tx]):
                return False
            if await self._validate_and_send_bundle([flashloan_tx, front_run_tx]):
                logger.info("Front-run executed successfully")
                return True
            return False
        except Exception as e:
            self.handle_error(e, "front_run", {"target_tx": target_tx})
            return False

    async def back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a back-run strategy on the target transaction.
        """
        valid, decoded_tx, _ = await self._validate_transaction(target_tx, "back-run")
        if not valid:
            return False

        try:
            back_run_tx = await self._prepare_back_run_transaction(target_tx, decoded_tx)
            if not back_run_tx:
                return False
            if await self._validate_and_send_bundle([back_run_tx]):
                logger.info("Back-run executed successfully")
                return True
            return False
        except Exception as e:
            self.handle_error(e, "back_run", {"target_tx": target_tx})
            return False

    async def execute_sandwich_attack(self, target_tx: Dict[str, Any], strategy: str = "default") -> bool:
        """
        Execute a sandwich attack strategy with configurable sub-strategies.
        """
        logger.debug(f"Initiating {strategy} sandwich attack strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "sandwich_attack")
        if not valid:
            return False

        try:
            should_execute = await self._check_sandwich_strategy(strategy, target_tx, token_symbol, decoded_tx)
            if not should_execute:
                logger.debug(f"Conditions not met for {strategy} sandwich strategy")
                return False

            path = decoded_tx["params"]["path"]
            flashloan_tx = await self._prepare_flashloan(path[0], target_tx)
            front_tx = await self._prepare_front_run_transaction(target_tx)
            back_tx = await self._prepare_back_run_transaction(target_tx, decoded_tx)

            if not all([flashloan_tx, front_tx, back_tx]):
                logger.warning("Failed to prepare all sandwich components")
                return False

            return await self._validate_and_send_bundle([flashloan_tx, front_tx, back_tx])
        except Exception as e:
            self.handle_error(e, "execute_sandwich_attack", {"target_tx": target_tx, "strategy": strategy})
            return False

    async def _check_sandwich_strategy(
        self, 
        strategy: str, 
        target_tx: Dict[str, Any], 
        token_symbol: str,
        decoded_tx: Dict[str, Any]
    ) -> bool:
        """
        Check if the conditions are met for a given sandwich attack strategy.
        """
        if strategy == "flash_profit":
            estimated_amount = await self.calculate_flashloan_amount(target_tx)
            estimated_profit = estimated_amount * Decimal(str(self.configuration.FLASHLOAN_BACK_RUN_PROFIT_PERCENTAGE))
            gas_price = await SafetyNet.get_dynamic_gas_price()
            return (estimated_profit > self.configuration.MIN_PROFIT and 
                    gas_price <= self.configuration.SANDWICH_ATTACK_GAS_PRICE_THRESHOLD_GWEI)

        elif strategy == "price_boost":
            historical_prices = await self.apiconfig.get_token_price_data(token_symbol, 'historical')
            if not historical_prices:
                return False
            momentum = await self._analyze_price_momentum(historical_prices)
            return momentum > self.configuration.PRICE_BOOST_SANDWICH_MOMENTUM_THRESHOLD

        elif strategy == "arbitrage":
            return await self.marketmonitor._is_arbitrage_opportunity(target_tx)

        elif strategy == "advanced":
            market_conditions = await self.marketmonitor.check_market_conditions(target_tx["to"])
            return (market_conditions.get("high_volatility", False) and 
                    market_conditions.get("bullish_trend", False))
        return True

    async def _prepare_flashloan(self, asset: str, target_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Prepare a flashloan transaction based on the target transaction.
        """
        flashloan_amount = self.calculate_flashloan_amount(target_tx)
        if flashloan_amount <= 0:
            return None
        return await self.prepare_flashloan_transaction(self.web3.to_checksum_address(asset), flashloan_amount)

    async def _validate_and_send_bundle(self, transactions: List[Dict[str, Any]]) -> bool:
        """
        Validate and send a bundle of transactions after simulating each one.
        """
        simulations = await asyncio.gather(
            *[self.simulate_transaction(tx) for tx in transactions],
            return_exceptions=True
        )
        if any(isinstance(result, Exception) or not result for result in simulations):
            logger.warning("Transaction simulation failed")
            return False
        return await self.send_bundle(transactions)

    async def _prepare_front_run_transaction(self, target_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Prepare a front-run transaction based on the target transaction.
        """
        try:
            decoded_tx = await self.decode_transaction_input(
                target_tx.get("input", "0x"),
                self.web3.to_checksum_address(target_tx.get("to", ""))
            )
            if not decoded_tx:
                logger.debug("Failed to decode target transaction input for front-run. Skipping.")
                return None

            function_name = decoded_tx.get("function_name")
            if not function_name:
                logger.debug("Missing function name in decoded transaction.")
                return None

            function_params = decoded_tx.get("params", {})
            to_address = self.web3.to_checksum_address(target_tx.get("to", ""))

            routers = {
                self.configuration.UNISWAP_ADDRESS: (self.uniswap_router_contract, "Uniswap"),
                self.configuration.SUSHISWAP_ADDRESS: (self.sushiswap_router_contract, "Sushiswap"),
            }
            if to_address not in routers:
                logger.warning(f"Unknown router address {to_address}. Cannot determine exchange.")
                return None

            router_contract, exchange_name = routers[to_address]
            if not router_contract:
                logger.warning(f"Router contract not initialized for {exchange_name}.")
                return None

            try:
                front_run_function = getattr(router_contract.functions, function_name)(**function_params)
            except AttributeError:
                logger.debug(f"Function {function_name} not found in {exchange_name} router ABI.")
                return None

            front_run_tx = await self.build_transaction(front_run_function)
            logger.info(f"Prepared front-run transaction on {exchange_name} successfully.")
            return front_run_tx
        except Exception as e:
            self.handle_error(e, "_prepare_front_run_transaction", {"target_tx": target_tx})
            return None

    async def _prepare_back_run_transaction(self, target_tx: Dict[str, Any], decoded_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Prepare a back-run transaction by reversing path parameters.
        """
        try:
            function_name = decoded_tx.get("function_name")
            if not function_name:
                logger.debug("Missing function name in decoded transaction.")
                return None

            function_params = decoded_tx.get("params", {})
            path = function_params.get("path", [])
            if not path or not isinstance(path, list) or len(path) < 2:
                logger.debug("Transaction has invalid or no path parameter for back-run.")
                return None

            function_params["path"] = path[::-1]
            to_address = self.web3.to_checksum_address(target_tx.get("to", ""))

            routers = {
                self.configuration.UNISWAP_ADDRESS: (self.uniswap_router_contract, "Uniswap"),
                self.configuration.SUSHISWAP_ADDRESS: (self.sushiswap_router_contract, "Sushiswap"),
            }
            if to_address not in routers:
                logger.debug(f"Unknown router address {to_address}. Cannot determine exchange.")
                return None

            router_contract, exchange_name = routers[to_address]
            if not router_contract:
                logger.debug(f"Router contract not initialized for {exchange_name}.")
                return None

            try:
                back_run_function = getattr(router_contract.functions, function_name)(**function_params)
            except AttributeError:
                logger.debug(f"Function {function_name} not found in {exchange_name} router ABI.")
                return None

            back_run_tx = await self.build_transaction(back_run_function)
            logger.info(f"Prepared back-run transaction on {exchange_name} successfully.")
            return back_run_tx
        except Exception as e:
            self.handle_error(e, "_prepare_back_run_transaction", {"target_tx": target_tx, "decoded_tx": decoded_tx})
            return None

    async def decode_transaction_input(self, input_data: str, contract_address: str) -> Optional[Dict[str, Any]]:
        """
        Decode a transaction input using the ABI registry.
        Note: Selector is now correctly extracted from index 2 to 10.
        """
        try:
            selector = input_data[2:10]  # Corrected slicing to extract 4-byte selector
            method_name = self.abiregistry.get_method_selector(selector)
            if not method_name:
                logger.debug(f"Unknown method selector: {selector}")
                return None

            for abi_type, abi in self.abiregistry.abis.items():
                try:
                    contract = self.web3.eth.contract(
                        address=self.web3.to_checksum_address(contract_address),
                        abi=abi
                    )
                    func_obj, decoded_params = contract.decode_function_input(input_data)
                    return {
                        "function_name": method_name,
                        "params": decoded_params,
                        "signature": selector,
                        "abi_type": abi_type
                    }
                except Exception:
                    continue
            return None
        except Exception as e:
            self.handle_error(e, "decode_transaction_input", {"input_data": input_data, "contract_address": contract_address})
            return None

    async def cancel_transaction(self, nonce: int) -> bool:
        """
        Cancel a pending transaction by sending a zero-value transaction with the same nonce.
        """
        cancel_tx = {
            "to": self.account.address,
            "value": 0,
            "gas": 21_000,
            "gasPrice": self.web3.to_wei(self.configuration.DEFAULT_CANCEL_GAS_PRICE_GWEI, "gwei"),
            "nonce": nonce,
            "chainId": await self.web3.eth.chain_id,
            "from": self.account.address,
        }
        try:
            signed_cancel_tx = await self.sign_transaction(cancel_tx)
            tx_hash = await self.web3.eth.send_raw_transaction(signed_cancel_tx)
            tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, hexbytes.HexBytes) else tx_hash
            logger.debug(f"Cancellation transaction sent successfully: {tx_hash_hex}")
            return True
        except Exception as e:
            self.handle_error(e, "cancel_transaction", {"nonce": nonce})
            return False

    async def estimate_gas_limit(self, tx: Dict[str, Any]) -> int:
        """
        (Deprecated: Use estimate_gas_smart instead)
        """
        return await self.estimate_gas_smart(tx)

    async def get_current_profit(self) -> Decimal:
        """
        Retrieve the current profit from the safety net.
        """
        try:
            current_profit = await self.safetynet.get_balance(self.account)
            self.current_profit = Decimal(current_profit)
            logger.debug(f"Current profit: {self.current_profit} ETH")
            return self.current_profit
        except Exception as e:
            self.handle_error(e, "get_current_profit")
            return Decimal("0")

    async def withdraw_eth(self) -> bool:
        """
        Withdraw ETH from the flashloan contract.
        """
        try:
            withdraw_function = self.aave_flashloan.functions.withdrawETH()
            tx = await self.build_transaction(withdraw_function)
            tx_hash = await self.execute_transaction(tx)
            if tx_hash:
                logger.debug(f"ETH withdrawal transaction sent with hash: {tx_hash}")
                return True
            else:
                logger.warning("Failed to send ETH withdrawal transaction.")
                return False
        except (ContractLogicError, Exception) as e:
            self.handle_error(e, "withdraw_eth")
            return False

    async def transfer_profit_to_account(self, amount: Decimal, account: str) -> bool:
        """
        Transfer a portion of the profit to another account.
        """
        try:
            transfer_function = self.aave_flashloan.functions.transfer(
                self.web3.to_checksum_address(account), int(amount * Decimal(self.DEFAULT_PROFIT_TRANSFER_MULTIPLIER))
            )
            tx = await self.build_transaction(transfer_function)
            tx_hash = await self.execute_transaction(tx)
            if tx_hash:
                logger.debug(f"Profit transfer transaction sent with hash: {tx_hash}")
                return True
            else:
                logger.warning("Failed to send profit transfer transaction.")
                return False
        except (ContractLogicError, Exception) as e:
            self.handle_error(e, "transfer_profit_to_account", {"amount": amount, "account": account})
            return False

    async def stop(self) -> None:
        """
        Stop all operations of the TransactionCore, including safety net and nonce core.
        """
        try:
            await self.safetynet.stop()
            await self.noncecore.stop()
            logger.debug("Stopped TransactionCore.")
        except Exception as e:
            self.handle_error(e, "stop")
            raise

    async def calculate_gas_parameters(self, tx: Dict[str, Any], gas_limit: Optional[int] = None) -> Dict[str, int]:
        """
        Calculate and return centralized gas parameters for a transaction.
        """
        try:
            gas_params = await self._get_dynamic_gas_parameters()
            estimated_gas = gas_limit or await self.estimate_gas_smart(tx)
            return {
                'gasPrice': gas_params['gasPrice'],
                'gas': int(estimated_gas * 1.1)
            }
        except Exception as e:
            self.handle_error(e, "calculate_gas_parameters", {"tx": tx, "gas_limit": gas_limit})
            return {
                "gasPrice": int(self.web3.to_wei(self.DEFAULT_GAS_PRICE_GWEI * self.gas_price_multiplier, "gwei")),
                "gas": self.DEFAULT_GAS_LIMIT
            }

    async def execute_transaction_with_gas_parameters(self, tx: Dict[str, Any], gas_params: Dict[str, int]) -> Optional[str]:
        """
        Execute a transaction after updating it with provided gas parameters.
        """
        try:
            tx.update(gas_params)
            return await self.execute_transaction(tx)
        except Exception as e:
            self.handle_error(e, "execute_transaction_with_gas_parameters", {"tx": tx, "gas_params": gas_params})
            return None

    async def call_contract_function(self, signed_tx: bytes) -> hexbytes.HexBytes:
        """
        Send a signed transaction to the blockchain.
        """
        try:
            return await self.web3.eth.send_raw_transaction(signed_tx)
        except Exception as e:
            self.handle_error(e, "call_contract_function", {"signed_tx": signed_tx})
            raise

    def handle_error(self, error: Exception, function_name: str, params: Optional[Dict[str, Any]] = None) -> None:
        """
        Centralized error handling for logging errors.
        """
        error_message = f"Error in {function_name}: {error}"
        if params:
            error_message += f" | Parameters: {params}"
        logger.error(error_message)

    async def aggressive_front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute an aggressive front-run strategy with dynamic gas pricing and risk assessment.
        """
        logger.debug("Initiating Aggressive Front-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(
            target_tx, "front_run", min_value=self.configuration.AGGRESSIVE_FRONT_RUN_MIN_VALUE_ETH 
        )
        if not valid:
            return False

        # Removed extra parameter (token_symbol) from risk score calculation.
        risk_score, market_conditions = await self._calculate_risk_score(
            target_tx,
            price_change=await self.apiconfig.get_price_change_24h(token_symbol)
        )

        # Normalize risk_score from 0-100 to 0-1.
        normalized_risk = risk_score / 100.0

        if normalized_risk >= self.configuration.AGGRESSIVE_FRONT_RUN_RISK_SCORE_THRESHOLD:
            logger.debug(f"Executing aggressive front-run (Risk: {normalized_risk:.2f})")
            return await self.front_run(target_tx)

        return False

    async def predictive_front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a predictive front-run strategy using advanced price prediction and market indicators.
        """
        logger.debug("Initiating Predictive Front-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "front_run")
        if not valid:
            return False

        try:
            data = await asyncio.gather(
                self.marketmonitor.predict_price_movement(token_symbol),
                self.apiconfig.get_real_time_price(token_symbol),
                self.marketmonitor.check_market_conditions(target_tx["to"]),
                self.apiconfig.get_token_price_data(token_symbol, 'historical', timeframe=1),
                return_exceptions=True
            )
            predicted_price, current_price, market_conditions, historical_prices = data

            if any(isinstance(x, Exception) for x in data) or current_price is None or predicted_price is None:
                logger.debug("Incomplete market data for predictive front-run.")
                return False

        except Exception as e:
            logger.error(f"Error gathering market data: {e}")
            return False

        price_change = (predicted_price / float(current_price) - 1) * 100
        volatility = np.std(historical_prices) / np.mean(historical_prices) if historical_prices else 0

        opportunity_score = await self._calculate_opportunity_score(
            price_change=price_change,
            volatility=volatility,
            market_conditions=market_conditions,
            current_price=current_price,
            historical_prices=historical_prices
        )

        logger.debug(
            f"Predictive Analysis for {token_symbol}:\n"
            f"Current Price: {current_price:.6f}\n"
            f"Predicted Price: {predicted_price:.6f}\n"
            f"Expected Change: {price_change:.2f}%\n"
            f"Volatility: {volatility:.2f}\n"
            f"Opportunity Score: {opportunity_score}/100\n"
            f"Market Conditions: {market_conditions}"
        )

        if opportunity_score >= self.configuration.FRONT_RUN_OPPORTUNITY_SCORE_THRESHOLD:
            logger.debug(
                f"Executing predictive front-run for {token_symbol} "
                f"(Score: {opportunity_score}/100, Expected Change: {price_change:.2f}%)"
            )
            return await self.front_run(target_tx)

        logger.debug(f"Opportunity score {opportunity_score}/100 below threshold. Skipping front-run.")
        return False

    async def volatility_front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a front-run strategy based on market volatility analysis.
        """
        logger.debug("Initiating Volatility Front-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "front_run")
        if not valid:
            return False

        try:
            results = await asyncio.gather(
                self.marketmonitor.check_market_conditions(target_tx["to"]),
                self.apiconfig.get_real_time_price(token_symbol),
                self.apiconfig.get_token_price_data(token_symbol, 'historical', timeframe=1),
                return_exceptions=True
            )
            market_conditions, current_price, historical_prices = results
            if any(isinstance(result, Exception) for result in results):
                logger.warning("Incomplete market data for volatility front-run")
                return False
        except Exception as e:
            logger.error(f"Error gathering market data: {e}")
            return False

        volatility_score = await self._calculate_volatility_score(
            historical_prices=historical_prices,
            current_price=current_price,
            market_conditions=market_conditions
        )

        logger.debug(
            f"Volatility Analysis for {token_symbol}:\n"
            f"Volatility Score: {volatility_score:.2f}/100\n"
            f"Current Price: {current_price}\n"
            f"24h Price Range: {min(historical_prices):.4f} - {max(historical_prices):.4f}\n"
            f"Market Conditions: {market_conditions}"
        )

        if volatility_score >= self.configuration.VOLATILITY_FRONT_RUN_SCORE_THRESHOLD:
            logger.debug(f"Executing volatility-based front-run for {token_symbol} (Volatility Score: {volatility_score:.2f}/100)")
            return await self.front_run(target_tx)

        logger.debug(f"Volatility score {volatility_score:.2f}/100 below threshold. Skipping front-run.")
        return False

    async def price_dip_back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a back-run strategy based on a predicted price dip.
        """
        logger.debug("Initiating Price Dip Back-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "back_run")
        if not valid:
            return False

        current_price = await self.apiconfig.get_real_time_price(token_symbol)
        if current_price is None:
            return False

        predicted_price = await self.marketmonitor.predict_price_movement(token_symbol)
        if predicted_price < float(current_price) * self.configuration.PRICE_DIP_BACK_RUN_THRESHOLD:
            logger.debug("Predicted price decrease meets threshold, proceeding with back-run.")
            return await self.back_run(target_tx)

        logger.debug("Predicted price decrease does not meet threshold. Skipping back-run.")
        return False

    async def flashloan_back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a flashloan-enabled back-run strategy.
        """
        logger.debug("Initiating Flashloan Back-Run Strategy...")
        estimated_amount = await self.calculate_flashloan_amount(target_tx)
        estimated_profit = estimated_amount * Decimal(str(self.configuration.FLASHLOAN_BACK_RUN_PROFIT_PERCENTAGE))
        if estimated_profit > self.configuration.MIN_PROFIT:
            logger.debug(f"Estimated profit: {estimated_profit} ETH meets threshold.")
            return await self.back_run(target_tx)
        logger.debug("Profit is insufficient for flashloan back-run. Skipping.")
        return False

    async def high_volume_back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a back-run strategy when high trading volume is detected.
        """
        logger.debug("Initiating High Volume Back-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "back_run")
        if not valid:
            return False

        volume_24h = await self.apiconfig.get_token_volume(token_symbol)
        volume_threshold = self._get_volume_threshold(token_symbol)
        if volume_24h > volume_threshold:
            logger.debug(f"High volume detected (${volume_24h:,.2f} USD), proceeding with back-run.")
            return await self.back_run(target_tx)

        logger.debug(f"Volume (${volume_24h:,.2f} USD) below threshold (${volume_threshold:,.2f} USD). Skipping.")
        return False

    async def advanced_sandwich_attack(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute an advanced sandwich attack strategy with integrated risk management.
        """
        logger.debug("Initiating Advanced Sandwich Attack...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "sandwich_attack")
        if not valid:
            return False

        market_conditions = await self.marketmonitor.check_market_conditions(target_tx["to"])
        if market_conditions.get("high_volatility", False) and market_conditions.get("bullish_trend", False):
            logger.debug("Conditions favorable for sandwich attack.")
            return await self.execute_sandwich_attack(target_tx)
        logger.debug("Conditions unfavorable for sandwich attack. Skipping.")
        return False

    async def _calculate_opportunity_score(
        self,
        price_change: float,
        volatility: float,
        market_conditions: Dict[str, bool],
        current_price: float, 
        historical_prices: List[float]
    ) -> float:
        """
        Calculate a comprehensive opportunity score (0-100) for front-run strategies based on multiple metrics.
        """
        score = 0
        components = {
           "price_change": {
               "very_strong": {"threshold": 5.0, "points": 40},
               "strong": {"threshold": 3.0, "points": 30},
               "moderate": {"threshold": 1.0, "points": 20},
               "slight": {"threshold": 0.5, "points": 10}
           },
           "volatility": {
               "very_low": {"threshold": 0.02, "points": 20},
               "low": {"threshold": 0.05, "points": 15},
               "moderate": {"threshold": 0.08, "points": 10},
           },
           "market_conditions": {
                "bullish_trend": {"points": 10},
                "not_high_volatility": {"points": 5},
                "not_low_liquidity": {"points": 5},
           },
            "price_trend": {
                "upward": {"points": 20},
                "stable": {"points": 10},
           }
       }

        if price_change > components["price_change"]["very_strong"]["threshold"]:
            score += components["price_change"]["very_strong"]["points"]
        elif price_change > components["price_change"]["strong"]["threshold"]:
            score += components["price_change"]["strong"]["points"]
        elif price_change > components["price_change"]["moderate"]["threshold"]:
            score += components["price_change"]["moderate"]["points"]
        elif price_change > components["price_change"]["slight"]["threshold"]:
            score += components["price_change"]["slight"]["points"]

        if historical_prices:
            avg_price = sum(historical_prices) / len(historical_prices)
            if current_price > avg_price * 1.1:
                score += 10
            elif current_price > avg_price * 1.05:
                score += 5

        if volatility < components["volatility"]["very_low"]["threshold"]:
           score += components["volatility"]["very_low"]["points"]
        elif volatility < components["volatility"]["low"]["threshold"]:
           score += components["volatility"]["low"]["points"]
        elif volatility < components["volatility"]["moderate"]["threshold"]:
           score += components["volatility"]["moderate"]["points"]

        if market_conditions.get("bullish_trend", False):
            score += components["market_conditions"]["bullish_trend"]["points"]
        if not market_conditions.get("high_volatility", True):
            score += components["market_conditions"]["not_high_volatility"]["points"]
        if not market_conditions.get("low_liquidity", True):
            score += components["market_conditions"]["not_low_liquidity"]["points"]

        if historical_prices and len(historical_prices) > 1:
            recent_trend = (historical_prices[-1] / historical_prices[0] - 1) * 100
            if recent_trend > 0:
                score += components["price_trend"]["upward"]["points"]
            elif recent_trend > -1:
                score += components["price_trend"]["stable"]["points"]

        logger.debug(f"Calculated opportunity score: {score}/100")
        return score

    async def _calculate_volatility_score(
        self,
        historical_prices: List[float],
        current_price: float,
        market_conditions: Dict[str, bool]
    ) -> float:
        """
        Calculate a volatility score (0-100) based on historical price data and market conditions.
        """
        score = 0
        components = {
            "historical_volatility": {
                "very_high": {"threshold": 0.1, "points": 40},
                "high": {"threshold": 0.08, "points": 30},
                "moderate": {"threshold": 0.05, "points": 20},
                "low": {"threshold": 0.03, "points": 10}
            },
            "price_range": {
                "very_wide": {"threshold": 0.2, "points": 30},
                "wide": {"threshold": 0.15, "points": 20},
                "moderate": {"threshold": 0.1, "points": 10}
            },
            "market_conditions": {
                "high_volatility": {"points": 20},
                "low_liquidity": {"points": 10}
            }
        }

        if historical_prices and len(historical_prices) > 1:
            historical_volatility = np.std(historical_prices) / np.mean(historical_prices)
            if historical_volatility > components["historical_volatility"]["very_high"]["threshold"]:
                score += components["historical_volatility"]["very_high"]["points"]
            elif historical_volatility > components["historical_volatility"]["high"]["threshold"]:
                score += components["historical_volatility"]["high"]["points"]
            elif historical_volatility > components["historical_volatility"]["moderate"]["threshold"]:
                score += components["historical_volatility"]["moderate"]["points"]
            elif historical_volatility > components["historical_volatility"]["low"]["threshold"]:
                score += components["historical_volatility"]["low"]["points"]

        if historical_prices:
            price_range = (max(historical_prices) - min(historical_prices)) / np.mean(historical_prices)
            if price_range > components["price_range"]["very_wide"]["threshold"]:
                score += components["price_range"]["very_wide"]["points"]
            elif price_range > components["price_range"]["wide"]["threshold"]:
                score += components["price_range"]["wide"]["points"]
            elif price_range > components["price_range"]["moderate"]["threshold"]:
                score += components["price_range"]["moderate"]["points"]

        if market_conditions.get("high_volatility", False):
            score += components["market_conditions"]["high_volatility"]["points"]
        if market_conditions.get("low_liquidity", False):
            score += components["market_conditions"]["low_liquidity"]["points"]

        logger.debug(f"Calculated volatility score: {score}/100")
        return score

    async def _calculate_risk_score(
        self,
        target_tx: Dict[str, Any],
        price_change: float
    ) -> Tuple[float, Dict[str, bool]]:
        """
        Calculate a risk score (0-1) for the target transaction based on price change, gas price, and market conditions.
        The raw score (0-100) is normalized to a fraction.
        """
        score = 0
        components = {
            "price_change": {
                "very_high": {"threshold": 10.0, "points": 40},
                "high": {"threshold": 7.0, "points": 30},
                "moderate": {"threshold": 4.0, "points": 20},
                "low": {"threshold": 2.0, "points": 10}
            },
            "gas_price": {
                "very_high": {"threshold": 200, "points": 30},
                "high": {"threshold": 150, "points": 20},
                "moderate": {"threshold": 100, "points": 10}
            },
            "market_conditions": {
                "high_volatility": {"points": 20},
                "low_liquidity": {"points": 10}
            }
        }

        if price_change > components["price_change"]["very_high"]["threshold"]:
            score += components["price_change"]["very_high"]["points"]
        elif price_change > components["price_change"]["high"]["threshold"]:
            score += components["price_change"]["high"]["points"]
        elif price_change > components["price_change"]["moderate"]["threshold"]:
            score += components["price_change"]["moderate"]["points"]
        elif price_change > components["price_change"]["low"]["threshold"]:
            score += components["price_change"]["low"]["points"]

        gas_price = await self.safetynet.get_dynamic_gas_price()
        if gas_price > components["gas_price"]["very_high"]["threshold"]:
            score += components["gas_price"]["very_high"]["points"]
        elif gas_price > components["gas_price"]["high"]["threshold"]:
            score += components["gas_price"]["high"]["points"]
        elif gas_price > components["gas_price"]["moderate"]["threshold"]:
            score += components["gas_price"]["moderate"]["points"]

        market_conditions = await self.marketmonitor.check_market_conditions(target_tx["to"])
        if market_conditions.get("high_volatility", False):
            score += components["market_conditions"]["high_volatility"]["points"]
        if market_conditions.get("low_liquidity", False):
            score += components["market_conditions"]["low_liquidity"]["points"]

        logger.debug(f"Calculated raw risk score: {score}/100")
        normalized_score = score / 100.0
        return normalized_score, market_conditions

    def _get_volume_threshold(self, token_symbol: str) -> float:
        """
        Retrieve the volume threshold for high volume back-run strategies based on the token symbol.
        """
        volume_thresholds = {
            "ETH": 1000000.0,
            "BTC": 500000.0,
            "USDT": 200000.0,
            "BNB": 100000.0,
            "ADA": 50000.0
        }
        return volume_thresholds.get(token_symbol, 10000.0)

    async def _analyze_price_momentum(self, historical_prices: List[float]) -> float:
        """
        Analyze price momentum based on historical price data.
        """
        if len(historical_prices) < 2:
            return 0.0
        return (historical_prices[-1] / historical_prices[0] - 1) * 100

    async def _is_contract_address(self, address: str) -> bool:
        """
        Check if an address is a contract address.
        """
        code = await self.web3.eth.get_code(self.web3.to_checksum_address(address))
        is_contract = code != '0x'
        if is_contract:
            logger.debug(f"Address {address} is a valid contract address.")
        return is_contract

    async def _validate_contract_interaction(self, tx: Dict[str, Any]) -> bool:
        """
        Validate whether a transaction interacts with a known contract address.
        """
        to_address = tx.get("to", "")
        if not to_address:
            return False
        is_contract = await self._is_contract_address(to_address)
        if not is_contract:
            logger.debug(f"Address {to_address} is not a contract address.")
            return False
        logger.debug(f"Address {to_address} is a valid contract address.")
        return True

    async def aggressive_front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute an aggressive front-run strategy with dynamic gas pricing and risk assessment.
        """
        logger.debug("Initiating Aggressive Front-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(
            target_tx, "front_run", min_value=self.configuration.AGGRESSIVE_FRONT_RUN_MIN_VALUE_ETH 
        )
        if not valid:
            return False

        risk_score, market_conditions = await self._calculate_risk_score(
            target_tx,
            price_change=await self.apiconfig.get_price_change_24h(token_symbol)
        )

        if risk_score >= self.configuration.AGGRESSIVE_FRONT_RUN_RISK_SCORE_THRESHOLD:
            logger.debug(f"Executing aggressive front-run (Risk: {risk_score:.2f})")
            return await self.front_run(target_tx)

        return False

    async def predictive_front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a predictive front-run strategy using advanced price prediction and market indicators.
        """
        logger.debug("Initiating Predictive Front-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "front_run")
        if not valid:
            return False

        try:
            data = await asyncio.gather(
                self.marketmonitor.predict_price_movement(token_symbol),
                self.apiconfig.get_real_time_price(token_symbol),
                self.marketmonitor.check_market_conditions(target_tx["to"]),
                self.apiconfig.get_token_price_data(token_symbol, 'historical', timeframe=1),
                return_exceptions=True
            )
            predicted_price, current_price, market_conditions, historical_prices = data

            if any(isinstance(x, Exception) for x in data) or current_price is None or predicted_price is None:
                logger.debug("Incomplete market data for predictive front-run.")
                return False

        except Exception as e:
            logger.error(f"Error gathering market data: {e}")
            return False

        price_change = (predicted_price / float(current_price) - 1) * 100
        volatility = np.std(historical_prices) / np.mean(historical_prices) if historical_prices else 0

        opportunity_score = await self._calculate_opportunity_score(
            price_change=price_change,
            volatility=volatility,
            market_conditions=market_conditions,
            current_price=current_price,
            historical_prices=historical_prices
        )

        logger.debug(
            f"Predictive Analysis for {token_symbol}:\n"
            f"Current Price: {current_price:.6f}\n"
            f"Predicted Price: {predicted_price:.6f}\n"
            f"Expected Change: {price_change:.2f}%\n"
            f"Volatility: {volatility:.2f}\n"
            f"Opportunity Score: {opportunity_score}/100\n"
            f"Market Conditions: {market_conditions}"
        )

        if opportunity_score >= self.configuration.FRONT_RUN_OPPORTUNITY_SCORE_THRESHOLD:
            logger.debug(
                f"Executing predictive front-run for {token_symbol} "
                f"(Score: {opportunity_score}/100, Expected Change: {price_change:.2f}%)"
            )
            return await self.front_run(target_tx)

        logger.debug(f"Opportunity score {opportunity_score}/100 below threshold. Skipping front-run.")
        return False

    async def volatility_front_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a front-run strategy based on market volatility analysis.
        """
        logger.debug("Initiating Volatility Front-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "front_run")
        if not valid:
            return False

        try:
            results = await asyncio.gather(
                self.marketmonitor.check_market_conditions(target_tx["to"]),
                self.apiconfig.get_real_time_price(token_symbol),
                self.apiconfig.get_token_price_data(token_symbol, 'historical', timeframe=1),
                return_exceptions=True
            )
            market_conditions, current_price, historical_prices = results
            if any(isinstance(result, Exception) for result in results):
                logger.warning("Incomplete market data for volatility front-run")
                return False
        except Exception as e:
            logger.error(f"Error gathering market data: {e}")
            return False

        volatility_score = await self._calculate_volatility_score(
            historical_prices=historical_prices,
            current_price=current_price,
            market_conditions=market_conditions
        )

        logger.debug(
            f"Volatility Analysis for {token_symbol}:\n"
            f"Volatility Score: {volatility_score:.2f}/100\n"
            f"Current Price: {current_price}\n"
            f"24h Price Range: {min(historical_prices):.4f} - {max(historical_prices):.4f}\n"
            f"Market Conditions: {market_conditions}"
        )

        if volatility_score >= self.configuration.VOLATILITY_FRONT_RUN_SCORE_THRESHOLD:
            logger.debug(f"Executing volatility-based front-run for {token_symbol} (Volatility Score: {volatility_score:.2f}/100)")
            return await self.front_run(target_tx)

        logger.debug(f"Volatility score {volatility_score:.2f}/100 below threshold. Skipping front-run.")
        return False

    async def price_dip_back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a back-run strategy based on a predicted price dip.
        """
        logger.debug("Initiating Price Dip Back-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "back_run")
        if not valid:
            return False

        current_price = await self.apiconfig.get_real_time_price(token_symbol)
        if current_price is None:
            return False

        predicted_price = await self.marketmonitor.predict_price_movement(token_symbol)
        if predicted_price < float(current_price) * self.configuration.PRICE_DIP_BACK_RUN_THRESHOLD:
            logger.debug("Predicted price decrease meets threshold, proceeding with back-run.")
            return await self.back_run(target_tx)

        logger.debug("Predicted price decrease does not meet threshold. Skipping back-run.")
        return False

    async def flashloan_back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a flashloan-enabled back-run strategy.
        """
        logger.debug("Initiating Flashloan Back-Run Strategy...")
        estimated_amount = await self.calculate_flashloan_amount(target_tx)
        estimated_profit = estimated_amount * Decimal(str(self.configuration.FLASHLOAN_BACK_RUN_PROFIT_PERCENTAGE))
        if estimated_profit > self.configuration.MIN_PROFIT:
            logger.debug(f"Estimated profit: {estimated_profit} ETH meets threshold.")
            return await self.back_run(target_tx)
        logger.debug("Profit is insufficient for flashloan back-run. Skipping.")
        return False

    async def high_volume_back_run(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute a back-run strategy when high trading volume is detected.
        """
        logger.debug("Initiating High Volume Back-Run Strategy...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "back_run")
        if not valid:
            return False

        volume_24h = await self.apiconfig.get_token_volume(token_symbol)
        volume_threshold = self._get_volume_threshold(token_symbol)
        if volume_24h > volume_threshold:
            logger.debug(f"High volume detected (${volume_24h:,.2f} USD), proceeding with back-run.")
            return await self.back_run(target_tx)

        logger.debug(f"Volume (${volume_24h:,.2f} USD) below threshold (${volume_threshold:,.2f} USD). Skipping.")
        return False

    async def advanced_sandwich_attack(self, target_tx: Dict[str, Any]) -> bool:
        """
        Execute an advanced sandwich attack strategy with integrated risk management.
        """
        logger.debug("Initiating Advanced Sandwich Attack...")
        valid, decoded_tx, token_symbol = await self._validate_transaction(target_tx, "sandwich_attack")
        if not valid:
            return False

        market_conditions = await self.marketmonitor.check_market_conditions(target_tx["to"])
        if market_conditions.get("high_volatility", False) and market_conditions.get("bullish_trend", False):
            logger.debug("Conditions favorable for sandwich attack.")
            return await self.execute_sandwich_attack(target_tx)
        logger.debug("Conditions unfavorable for sandwich attack. Skipping.")
        return False

    async def _analyze_opportunity_score(
        self,
        price_change: float,
        volatility: float,
        market_conditions: Dict[str, bool],
        current_price: float, 
        historical_prices: List[float]
    ) -> float:
        # This helper is similar to _calculate_opportunity_score.
        return await self._calculate_opportunity_score(price_change, volatility, market_conditions, current_price, historical_prices)

    async def _calculate_volatility_score(
        self,
        historical_prices: List[float],
        current_price: float,
        market_conditions: Dict[str, bool]
    ) -> float:
        """
        Calculate a volatility score (0-100) based on historical price data and market conditions.
        """
        score = 0
        components = {
            "historical_volatility": {
                "very_high": {"threshold": 0.1, "points": 40},
                "high": {"threshold": 0.08, "points": 30},
                "moderate": {"threshold": 0.05, "points": 20},
                "low": {"threshold": 0.03, "points": 10}
            },
            "price_range": {
                "very_wide": {"threshold": 0.2, "points": 30},
                "wide": {"threshold": 0.15, "points": 20},
                "moderate": {"threshold": 0.1, "points": 10}
            },
            "market_conditions": {
                "high_volatility": {"points": 20},
                "low_liquidity": {"points": 10}
            }
        }

        if historical_prices and len(historical_prices) > 1:
            historical_volatility = np.std(historical_prices) / np.mean(historical_prices)
            if historical_volatility > components["historical_volatility"]["very_high"]["threshold"]:
                score += components["historical_volatility"]["very_high"]["points"]
            elif historical_volatility > components["historical_volatility"]["high"]["threshold"]:
                score += components["historical_volatility"]["high"]["points"]
            elif historical_volatility > components["historical_volatility"]["moderate"]["threshold"]:
                score += components["historical_volatility"]["moderate"]["points"]
            elif historical_volatility > components["historical_volatility"]["low"]["threshold"]:
                score += components["historical_volatility"]["low"]["points"]

        if historical_prices:
            price_range = (max(historical_prices) - min(historical_prices)) / np.mean(historical_prices)
            if price_range > components["price_range"]["very_wide"]["threshold"]:
                score += components["price_range"]["very_wide"]["points"]
            elif price_range > components["price_range"]["wide"]["threshold"]:
                score += components["price_range"]["wide"]["points"]
            elif price_range > components["price_range"]["moderate"]["threshold"]:
                score += components["price_range"]["moderate"]["points"]

        if market_conditions.get("high_volatility", False):
            score += components["market_conditions"]["high_volatility"]["points"]
        if market_conditions.get("low_liquidity", False):
            score += components["market_conditions"]["low_liquidity"]["points"]

        logger.debug(f"Calculated volatility score: {score}/100")
        return score

    async def _calculate_risk_score(
        self,
        target_tx: Dict[str, Any],
        price_change: float
    ) -> Tuple[float, Dict[str, bool]]:
        """
        (Already defined above; see aggressive_front_run for risk normalization.)
        """
        # This method has been defined earlier.
        pass  # (Placeholder if needed; otherwise, use the one above)

    def _get_volume_threshold(self, token_symbol: str) -> float:
        """
        Retrieve the volume threshold for high volume back-run strategies based on the token symbol.
        """
        volume_thresholds = {
            "ETH": 1000000.0,
            "BTC": 500000.0,
            "USDT": 200000.0,
            "BNB": 100000.0,
            "ADA": 50000.0
        }
        return volume_thresholds.get(token_symbol, 10000.0)

    async def _analyze_price_momentum(self, historical_prices: List[float]) -> float:
        """
        Analyze price momentum based on historical price data.
        """
        if len(historical_prices) < 2:
            return 0.0
        return (historical_prices[-1] / historical_prices[0] - 1) * 100

    async def _is_contract_address(self, address: str) -> bool:
        """
        Check if an address is a contract address.
        """
        code = await self.web3.eth.get_code(self.web3.to_checksum_address(address))
        is_contract = code != '0x'
        if is_contract:
            logger.debug(f"Address {address} is a valid contract address.")
        return is_contract

    async def _validate_contract_interaction(self, tx: Dict[str, Any]) -> bool:
        """
        Validate whether a transaction interacts with a known contract address.
        """
        to_address = tx.get("to", "")
        if not to_address:
            return False
        is_contract = await self._is_contract_address(to_address)
        if not is_contract:
            logger.debug(f"Address {to_address} is not a contract address.")
            return False
        logger.debug(f"Address {to_address} is a valid contract address.")
        return True
