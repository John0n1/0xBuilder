#========================================================================================================================
# https://github.com/John0n1/0xBuilder

import json
import aiofiles
import asyncio
from eth_utils import function_signature_to_4byte_selector
from pathlib import Path
from typing import Any, Dict, List, Optional


from loggingconfig import setup_logging
import logging

logger = setup_logging("AbiRegistry", level=logging.INFO)


class ABIRegistry:
    """
    Centralized ABI registry with loading, validation, and signature mapping.
    """

    REQUIRED_METHODS = {
        'erc20': {'transfer', 'approve', 'transferFrom', 'balanceOf'},
        'uniswap': {'swapExactTokensForTokens', 'swapTokensForExactTokens', 'addLiquidity', 'getAmountsOut'},
        'sushiswap': {'swapExactTokensForTokens', 'swapTokensForExactTokens', 'addLiquidity', 'getAmountsOut'},
        'aave_flashloan': {'fn_RequestFlashLoan', 'executeOperation', 'ADDRESSES_PROVIDER', 'POOL'},
        'aave': {'admin', 'implementation', 'upgradeToAndCall'}
    }

    def __init__(self) -> None:
        """Initialize the ABI Registry with empty data structures."""
        self.abis: Dict[str, List[Dict]] = {}
        self.signatures: Dict[str, Dict[str, str]] = {}
        self.method_selectors: Dict[str, Dict[str, str]] = {}
        self._initialized: bool = False

    async def initialize(self, base_path: Optional[Path] = None) -> None:
        """Asynchronously load and validate all ABIs."""
        if self._initialized:
            logger.debug("ABIRegistry already initialized.")
            return
        await self._load_all_abis(base_path)
        self._initialized = True
        logger.debug("ABIRegistry initialization complete.")

    async def _load_all_abis(self, base_path: Optional[Path] = None) -> None:
        """Load and validate all ABIs from JSON files."""
        if not base_path:
            base_path = Path(__file__).parent.parent
        abi_dir = base_path / 'abi'
        logger.debug(f"ABI Directory: {abi_dir}")

        abi_files = {
            'erc20': 'erc20_abi.json',
            'uniswap': 'uniswap_abi.json',
            'sushiswap': 'sushiswap_abi.json',
            'aave_flashloan': 'aave_flashloan_abi.json',
            'aave': 'aave_pool_abi.json'
        }

        critical_abis = {'erc20', 'uniswap'}

        await asyncio.gather(
            *[self._load_and_validate_abi(abi_type, abi_dir / filename, critical_abis) for abi_type, filename in abi_files.items()]
        )

    async def _load_and_validate_abi(self, abi_type: str, abi_path: Path, critical_abis: set) -> None:
        """Load and validate a single ABI."""
        try:
            abi = await self._load_abi_from_path(abi_path, abi_type)
            self.abis[abi_type] = abi
            self._extract_signatures(abi, abi_type)
            logger.info(f"Loaded and validated {abi_type} ABI from {abi_path}")
        except FileNotFoundError:
            logger.error(f"ABI file not found for {abi_type}: {abi_path}")
            if abi_type in critical_abis:
                raise
            else:
                logger.warning(f"Skipping non-critical ABI: {abi_type}")
        except ValueError as ve:
            logger.error(f"Validation failed for {abi_type} ABI: {ve}")
            if abi_type in critical_abis:
                raise
            else:
                logger.warning(f"Skipping non-critical ABI: {abi_type}")
        except json.JSONDecodeError as je:
            logger.error(f"JSON decode error for {abi_type} ABI: {je} in file: {abi_path}")
            if abi_type in critical_abis:
                raise
            else:
                logger.warning(f"Skipping non-critical ABI: {abi_type}")
        except Exception as e:
            logger.error(f"Unexpected error loading {abi_type} ABI from {abi_path}: {e}", exc_info=True)
            if abi_type in critical_abis:
                raise
            else:
                logger.warning(f"Skipping non-critical ABI: {abi_type}")

    async def _load_abi_from_path(self, abi_path: Path, abi_type: str) -> List[Dict[str, Any]]:
        """Load and validate ABI content from the specified path."""
        try:
            if not abi_path.exists():
                logger.error(f"ABI file not found: {abi_path}")
                raise FileNotFoundError(f"ABI file not found: {abi_path}")

            async with aiofiles.open(abi_path, 'r', encoding='utf-8') as f:
                abi_content = await f.read()
                abi = json.loads(abi_content)
                logger.debug(f"ABI content loaded from {abi_path}")

            if not self._validate_abi(abi, abi_type):
                raise ValueError(f"Validation failed for {abi_type} ABI from file {abi_path}")

            return abi
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for {abi_type} in file {abi_path}: {e}")
            raise
        except FileNotFoundError as e:
            logger.error(f"File not found error for {abi_type} in file {abi_path}: {e}")
            raise
        except ValueError as e:
            logger.error(f"Value error for {abi_type} in file {abi_path}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error loading ABI {abi_type} from {abi_path}: {e}", exc_info=True)
            raise

    def _validate_abi(self, abi: List[Dict[str, Any]], abi_type: str) -> bool:
        """Validate the structure and required methods of an ABI."""
        if not isinstance(abi, list):
            logger.error(f"Invalid ABI format for {abi_type}: ABI must be a list.")
            return False

        found_methods = {
            item.get('name') for item in abi
            if item.get('type') == 'function' and 'name' in item
        }

        required = self.REQUIRED_METHODS.get(abi_type, set())
        if not required.issubset(found_methods):
            missing = required - found_methods
            logger.error(f"Missing required methods in {abi_type} ABI: {missing}. Required methods are: {required}. Found methods are: {found_methods}") 
            return False

        return True

    def _extract_signatures(self, abi: List[Dict[str, Any]], abi_type: str) -> None:
        """Extract function signatures and method selectors from an ABI."""
        signatures = {}
        selectors = {}

        for item in abi:
            if item.get('type') == 'function':
                name = item.get('name')
                if name:
                    inputs = ','.join(inp.get('type', '') for inp in item.get('inputs', []))
                    signature = f"{name}({inputs})"
                    selector = function_signature_to_4byte_selector(signature)
                    hex_selector = selector.hex()

                    signatures[name] = signature
                    selectors[hex_selector] = name

        self.signatures[abi_type] = signatures
        self.method_selectors[abi_type] = selectors

    def get_abi(self, abi_type: str) -> Optional[List[Dict[str, Any]]]:
        """Retrieve a validated ABI by type."""
        return self.abis.get(abi_type)

    def get_method_selector(self, selector: str) -> Optional[str]:
        """Retrieve a method name from selector, checking all ABIs."""
        for abi_type, selectors in self.method_selectors.items():
            if selector in selectors:
                return selectors[selector]
        return None

    def get_function_signature(self, abi_type: str, method_name: str) -> Optional[str]:
        """Retrieve a function signature by ABI type and method name."""
        return self.signatures.get(abi_type, {}).get(method_name)

    async def update_abi(self, abi_type: str, new_abi: List[Dict[str, Any]]) -> None:
        """Update an ABI dynamically without restarting the application."""
        if not self._validate_abi(new_abi, abi_type):
            raise ValueError(f"Validation failed for {abi_type} ABI")

        self.abis[abi_type] = new_abi
        self._extract_signatures(new_abi, abi_type)
        logger.info(f"Updated {abi_type} ABI dynamically")
