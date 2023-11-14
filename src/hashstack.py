from typing import Dict, Optional
import copy
import decimal
import logging

import pandas

import src.constants
import src.helpers
import src.state



ADDRESS: str = "0x03dcf5c72ba60eb7b2fe151032769d49dd3df6b04fa3141dffd6e2aa162b7a6e"

# Keys are values of the "key_name" column in the database, values are the respective method names.
EVENTS_METHODS_MAPPING: Dict[str, str] = {
    "new_loan": "process_new_loan_event",
    "collateral_added": "process_collateral_added_event",
    "collateral_withdrawal": "process_collateral_withdrawal_event",
    "loan_withdrawal": "process_loan_withdrawal_event",
    "loan_repaid": "process_loan_repaid_event",
    "loan_swap": "process_loan_swap_event",
    "loan_interest_deducted": "process_loan_interest_deducted_event",
    "liquidated": "process_liquidated_event",
}

SUPPLY_TOKEN_ADRESSES: Dict[str, str] = {
    "ETH": "0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7",
    "wBTC": "0x03fe2b97c1fd336e750087d68b9b867997fd64a2661ff3ca5a7c771641e8e7ac",
    "USDC": "0x053c91253bc9682c04929ca02ed00b3e423f6710d2ee7e0d5ebb06f3ecf368a8",
    "DAI": "0x00da114221cb83fa859dbdb4c44beeaa0bb37c7537ad5ae66fe5e0efd20e6eb3",
    "USDT": "0x068f5c6a61780768455de69077e07e89787839bf8166decfbf92b645209c0fb8",
}

SUPPLY_HOLDER_ADRESSES: Dict[str, str] = {
    "ETH": "0x03dcf5c72ba60eb7b2fe151032769d49dd3df6b04fa3141dffd6e2aa162b7a6e",
    "wBTC": "0x03dcf5c72ba60eb7b2fe151032769d49dd3df6b04fa3141dffd6e2aa162b7a6e",
    "USDC": "0x03dcf5c72ba60eb7b2fe151032769d49dd3df6b04fa3141dffd6e2aa162b7a6e",
    "DAI": "0x03dcf5c72ba60eb7b2fe151032769d49dd3df6b04fa3141dffd6e2aa162b7a6e",
    "USDT": "0x03dcf5c72ba60eb7b2fe151032769d49dd3df6b04fa3141dffd6e2aa162b7a6e",
}



def get_events(start_block_number: int = 0) -> pandas.DataFrame:
    events = src.helpers.get_events(
        adresses = (ADDRESS, ''),
        events = tuple(EVENTS_METHODS_MAPPING),
        start_block_number = start_block_number,
    )
    # Ensure we're processing `loan_repaid` after other loan-altering events and the other events in a logical order.
    events["order"] = events["key_name"].map(
        {
            "new_loan": 0,
            "loan_withdrawal": 3,
            "loan_repaid": 4,
            "loan_swap": 1,
            "collateral_added": 6,
            "collateral_withdrawal": 7,
            "loan_interest_deducted": 5,
            "liquidated": 2,
        },
    )
    events.sort_values(
        ["block_number", "transaction_hash", "order"], inplace=True
    )
    return events


class HashstackLoanEntity(src.state.LoanEntity):
    """
    A class that describes the Hashstack loan entity. On top of the abstract `LoanEntity`, it implements the `user`, 
    `debt_category`, `original_collateral` and `borrowed_collateral` attributes in order to help with accounting for 
    the changes in collateral. This is because under Hashstack, each user can have multiple loans which are treated 
    completely separately (including liquidations). The `debt_category` attribute determines liquidation conditions.
    Also, because Hashstack provides leverage to its users, we split `collateral` into `original_collateral` 
    (collateral deposited by the user directly) and `borrowed_collateral` (the current state, i.e. token and amount of 
    the borrowed funds). We also use face amounts (no need to convert amounts using interest rates) because Hashstack 
    doesn't publish interest rate events.
    """

    # TODO: These are set to neutral values because Hashstack doesn't use collateral factors.
    COLLATERAL_FACTORS = {
        'ETH': decimal.Decimal('1'),
        'USDC': decimal.Decimal('1'),
        'USDT': decimal.Decimal('1'),
        'DAI': decimal.Decimal('1'),
        'wBTC': decimal.Decimal('1'),
        'wstETH': decimal.Decimal('1'),
    }

    def __init__(self, user: str, debt_category: int) -> None:
        super().__init__()
        self.user = user
        self.debt_category = debt_category
        self.original_collateral: src.state.TokenAmounts = src.state.TokenAmounts()
        self.borrowed_collateral: src.state.TokenAmounts = src.state.TokenAmounts()

    def compute_health_factor(
        self,
        prices: Optional[Dict[str, decimal.Decimal]] = None,
        collateral_usd: Optional[decimal.Decimal] = None,
        debt_usd: Optional[decimal.Decimal] = None,
    ) -> decimal.Decimal:
        if collateral_usd is None:
            collateral_usd = self.compute_collateral_usd(prices = prices)
        if debt_usd is None:
            debt_usd = self.compute_debt_usd(prices = prices)
        if debt_usd == decimal.Decimal("0"):
            # TODO: Assumes collateral is positive.
            return decimal.Decimal("Inf")
        return collateral_usd / debt_usd

    def compute_standardized_health_factor(
        self,
        prices: Optional[Dict[str, decimal.Decimal]] = None,
        collateral_usd: Optional[decimal.Decimal] = None,
        debt_usd: Optional[decimal.Decimal] = None,
    ) -> decimal.Decimal:
        if collateral_usd is None:
            collateral_usd = self.compute_collateral_usd(prices = prices)
        if debt_usd is None:
            debt_usd = self.compute_debt_usd(prices = prices)
        # Compute the value of (risk-adjusted) collateral at which the user/loan can be liquidated.
        health_factor_liquidation_threshold = (
            decimal.Decimal("1.06")
            if self.debt_category == 1
            else decimal.Decimal("1.05")
            if self.debt_category == 2
            else decimal.Decimal("1.04")
        )
        collateral_usd_threshold = health_factor_liquidation_threshold * debt_usd
        if collateral_usd_threshold == decimal.Decimal("0"):
            # TODO: Assumes collateral is positive.
            return decimal.Decimal("Inf")
        return collateral_usd / collateral_usd_threshold

    def compute_debt_to_be_liquidated(
        self,
        debt_usd: Optional[decimal.Decimal] = None,
        prices: Optional[Dict[str, decimal.Decimal]] = None,
    ) -> decimal.Decimal:
        if debt_usd is None:
            debt_usd = self.compute_debt_usd(prices = prices)
        return debt_usd


class HashstackState(src.state.State):
    """
    A class that describes the state of all Hashstack loan entities. It implements a method for correct processing of 
    every relevant event. Hashstack events always contain the final state of the loan entity's collateral and debt, 
    thus we always rewrite the balances whenever they are updated. 
    """

    EVENTS_METHODS_MAPPING: Dict[str, str] = EVENTS_METHODS_MAPPING

    def __init__(
        self,
        verbose_user: Optional[str] = None,
    ) -> None:
        super().__init__(
            loan_entity_class=HashstackLoanEntity,
            verbose_user=verbose_user,
        )

    # TODO: Reduce most of the events to `rewrite_original_collateral`, `rewrite_borrowed_collateral`, and 
    # `rewrite_debt`?

    def process_new_loan_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`loan_record`] `id`, `owner`, `market`, `commitment`, 
        # `amount`, ``, `current_market`, `current_amount`, ``, `is_loan_withdrawn`, `debt_category`, `state`, 
        # `l3_integration`, `created_at`, [`collateral`] `market`, `amount`, ``, `current_amount`, ``, `commitment`, 
        # `timelock_validity`, `is_timelock_activated`, `activation_time`, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x04ff9acb9154603f1fc14df328a3ea53a6c58087aaac0bfbe9cc7f2565777db8_2.
        loan_id = int(event["data"][0], base=16)
        user = event["data"][1]
        debt_token = src.constants.get_symbol(event["data"][2])
        debt_face_amount = decimal.Decimal(str(int(event["data"][4], base=16)))
        borrowed_collateral_token = src.constants.get_symbol(event["data"][6])
        borrowed_collateral_face_amount = decimal.Decimal(str(int(event["data"][7], base=16)))
        debt_category = int(event["data"][10], base=16)
        # Several initial loans have different structure of 'data'.
        try:
            original_collateral_token = src.constants.get_symbol(event["data"][14])
            original_collateral_face_amount = decimal.Decimal(str(int(event["data"][17], base=16)))
        except KeyError:
            original_collateral_token = src.constants.get_symbol(event["data"][13])
            original_collateral_face_amount = decimal.Decimal(str(int(event["data"][16], base=16)))

        self.loan_entities[loan_id] = HashstackLoanEntity(user=user, debt_category=debt_category)
        # TODO: Make it possible to initialize src.state.TokenAmounts with some token amount directly.
        original_collateral = src.state.TokenAmounts()
        original_collateral.token_amounts[original_collateral_token] = original_collateral_face_amount
        self.loan_entities[loan_id].original_collateral = original_collateral
        borrowed_collateral = src.state.TokenAmounts()
        borrowed_collateral.token_amounts[borrowed_collateral_token] = borrowed_collateral_face_amount
        self.loan_entities[loan_id].borrowed_collateral = borrowed_collateral
        # TODO: Make it easier to sum 2 src.state.TokenAmounts instances.
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        debt = src.state.TokenAmounts()
        debt.token_amounts[debt_token] = debt_face_amount
        self.loan_entities[loan_id].debt = debt
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, face amount = {} of token = {} was borrowed against original collateral face '
                'amount = {} of token = {} and borrowed collateral face amount = {} of token = {}.'.format(
                    event["block_number"],
                    debt_face_amount,
                    debt_token,
                    original_collateral_face_amount,
                    original_collateral_token,
                    original_collateral_token,
                    borrowed_collateral_face_amount,
                    borrowed_collateral_token,
                )
            )

    def process_collateral_added_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`collateral_record`] `market`, `amount`, ``, 
        # `current_amount`, ``, `commitment`, `timelock_validity`, `is_timelock_activated`, `activation_time`, 
        # [`loan_id`] `loan_id`, [`amount_added`] `amount_added`, ``, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x02df71b02fce15f2770533328d1e645b957ac347d96bd730466a2e087f24ee07_2.
        loan_id = int(event["data"][9], base=16)
        original_collateral_token = src.constants.get_symbol(event["data"][0])
        original_collateral_face_amount = decimal.Decimal(str(int(event["data"][3], base=16)))
        original_collateral = src.state.TokenAmounts()
        original_collateral.token_amounts[original_collateral_token] = original_collateral_face_amount
        self.loan_entities[loan_id].original_collateral = original_collateral
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, collateral was added, resulting in collateral of face amount = {} of token = '
                '{}.'.format(
                    event["block_number"],
                    original_collateral_face_amount,
                    original_collateral_token,
                )
            )

    def process_collateral_withdrawal_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`collateral_record`] `market`, `amount`, ``,
        # `current_amount`, ``, `commitment`, `timelock_validity`, `is_timelock_activated`, `activation_time`, 
        # [`loan_id`] `loan_id`, [`amount_withdrawn`] `amount_withdrawn`, ``, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x03809ebcaad1647f2c6d5294706e0dc619317c240b5554848c454683a18b75ba_5.
        loan_id = int(event["data"][9], base=16)
        original_collateral_token = src.constants.get_symbol(event["data"][0])
        original_collateral_face_amount = decimal.Decimal(str(int(event["data"][3], base=16)))
        original_collateral = src.state.TokenAmounts()
        original_collateral.token_amounts[original_collateral_token] = original_collateral_face_amount
        self.loan_entities[loan_id].original_collateral = original_collateral
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, collateral was withdrawn, resulting in collateral of face amount = {} of token '
                '= {}.'.format(
                    event["block_number"],
                    original_collateral_face_amount,
                    original_collateral_token,
                )
            )

    def process_loan_withdrawal_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`loan_record`] `id`, `owner`, `market`, `commitment`, 
        # `amount`, ``, `current_market`, `current_amount`, ``, `is_loan_withdrawn`, `debt_category`, `state`, 
        # `l3_integration`, `created_at`, [`amount_withdrawn`] `amount_withdrawn`, ``, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x05bb8614095fac1ac9b405c27e7ce870804e85aa5924ef2494fec46792b6b8dc_2.
        loan_id = int(event["data"][0], base = 16)
        user = event["data"][1]
        # TODO: Is this assert needed?
        assert self.loan_entities[loan_id].user == user
        debt_token = src.constants.get_symbol(event["data"][2])
        debt_face_amount = decimal.Decimal(str(int(event["data"][4], base=16)))
        borrowed_collateral_token = src.constants.get_symbol(event["data"][6])
        borrowed_collateral_face_amount = decimal.Decimal(str(int(event["data"][7], base=16)))
        debt_category = int(event["data"][10], base=16)

        borrowed_collateral = src.state.TokenAmounts()
        borrowed_collateral.token_amounts[borrowed_collateral_token] = borrowed_collateral_face_amount
        self.loan_entities[loan_id].borrowed_collateral = borrowed_collateral
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        debt = src.state.TokenAmounts()
        debt.token_amounts[debt_token] = debt_face_amount
        self.loan_entities[loan_id].debt = debt
        self.loan_entities[loan_id].debt_category = debt_category
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, loan was withdrawn, resulting in debt of face amount = {} of token = {} and '
                'borrowed collateral of face amount = {} of token = {}.'.format(
                    event["block_number"],
                    debt_face_amount,
                    debt_token,
                    borrowed_collateral_face_amount,
                    borrowed_collateral_token,
                )
            )

    def process_loan_repaid_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`loan_record`] `id`, `owner`, `market`, `commitment`, 
        # `amount`, ``, `current_market`, `current_amount`, ``, `is_loan_withdrawn`, `debt_category`, `state`, 
        # `l3_integration`, `created_at`, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x07731e48d33f6b916f4e4e81e9cee1d282e20e970717e11ad440f73cc1a73484_1.
        loan_id = int(event["data"][0], base = 16)
        user = event["data"][1]
        assert self.loan_entities[loan_id].user == user
        debt_token = src.constants.get_symbol(event["data"][2])
        # This prevents repaid loans to appear as not repaid.
        debt_face_amount = decimal.Decimal("0")
        borrowed_collateral_token = src.constants.get_symbol(event["data"][6])
        borrowed_collateral_face_amount = decimal.Decimal(str(int(event["data"][7], base=16)))
        # Based on the documentation, it seems that it's only possible to repay the whole amount.
        assert borrowed_collateral_face_amount == decimal.Decimal("0")
        debt_category = int(event["data"][10], base=16)

        borrowed_collateral = src.state.TokenAmounts()
        borrowed_collateral.token_amounts[borrowed_collateral_token] = borrowed_collateral_face_amount
        self.loan_entities[loan_id].borrowed_collateral = borrowed_collateral
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        debt = src.state.TokenAmounts()
        debt.token_amounts[debt_token] = debt_face_amount
        self.loan_entities[loan_id].debt = debt
        self.loan_entities[loan_id].debt_category = debt_category
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, loan was repaid, resulting in debt of face amount = {} of token = {} and '
                'borrowed collateral of face amount = {} of token = {}.'.format(
                    event["block_number"],
                    debt_face_amount,
                    debt_token,
                    borrowed_collateral_face_amount,
                    borrowed_collateral_token,
                )
            )

    def process_loan_swap_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`old_loan_record`] `id`, `owner`, `market`, `commitment`, 
        # `amount`, ``, `current_market`, `current_amount`, ``, `is_loan_withdrawn`, `debt_category`, `state`, 
        # `l3_integration`, `created_at`, [`new_loan_record`] `id`, `owner`, `market`, `commitment`, `amount`, ``, 
        # `current_market`, `current_amount`, ``, `is_loan_withdrawn`, `debt_category`, `state`, `l3_integration`, 
        # `created_at`, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x00ad0b6b00ce68a1d7f5b79cd550d7f4a15b1708b632b88985a4f6faeb42d5b1_7.
        old_loan_id = int(event["data"][0], base = 16)
        old_user = event["data"][1]
        assert self.loan_entities[old_loan_id].user == old_user
        new_loan_id = int(event["data"][14], base=16)
        new_user = event["data"][15]
        # TODO: Does this always have to hold?
        assert new_loan_id == old_loan_id
        # TODO: Does this always have to hold?
        assert new_user == old_user
        new_debt_token = src.constants.get_symbol(event["data"][16])
        new_debt_face_amount = decimal.Decimal(str(int(event["data"][18], base=16)))
        new_borrowed_collateral_token = src.constants.get_symbol(event["data"][20])
        new_borrowed_collateral_face_amount = decimal.Decimal(str(int(event["data"][21], base=16)))
        new_debt_category = int(event["data"][24], base=16)

        new_borrowed_collateral = src.state.TokenAmounts()
        new_borrowed_collateral.token_amounts[new_borrowed_collateral_token] = new_borrowed_collateral_face_amount
        self.loan_entities[new_loan_id].borrowed_collateral = new_borrowed_collateral
        self.loan_entities[new_loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[new_loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[new_loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        new_debt = src.state.TokenAmounts()
        new_debt.token_amounts[new_debt_token] = new_debt_face_amount
        # Based on the documentation, it seems that it's only possible to swap the whole amount.
        assert self.loan_entities[old_loan_id].debt.token_amounts == new_debt.token_amounts
        self.loan_entities[new_loan_id].debt = new_debt
        self.loan_entities[new_loan_id].debt_category = new_debt_category
        if self.loan_entities[new_loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, loan was swapped, resulting in debt of face amount = {} of token = {} and '
                'borrowed collateral of face amount = {} of token = {}.'.format(
                    event["block_number"],
                    new_debt_face_amount,
                    new_debt_token,
                    new_borrowed_collateral_face_amount,
                    new_borrowed_collateral_token,
                )
            )

    def process_loan_interest_deducted_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`collateral_record`] `market`, `amount`, ``,
        # `current_amount`, ``, `commitment`, `timelock_validity`, `is_timelock_activated`, `activation_time`, 
        # [`accrued_interest`] `accrued_interest`, ``, [`loan_id`] `loan_id`, [`amount_withdrawn`] `amount_withdrawn`,
        # ``, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x050db0ed93d7abbfb152e16608d4cf4dbe0b686b134f890dd0ad8418b203c580_2.
        loan_id = int(event["data"][11], base=16)
        original_collateral_token = src.constants.get_symbol(event["data"][0])
        original_collateral_face_amount = decimal.Decimal(str(int(event["data"][3], base=16)))
        original_collateral = src.state.TokenAmounts()
        original_collateral.token_amounts[original_collateral_token] = original_collateral_face_amount
        self.loan_entities[loan_id].original_collateral = original_collateral
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, loan interest was deducted, resulting in collateral of face amount = {} of '
                'token = {}.'.format(
                    event["block_number"],
                    original_collateral_face_amount,
                    original_collateral_token,
                )
            )

    def process_liquidated_event(self, event: pandas.Series) -> None:
        # The order of the values in the `data` column is: [`loan_record`] `id`, `owner`, `market`, `commitment`, 
        # `amount`, ``, `current_market`, `current_amount`, ``, `is_loan_withdrawn`, `debt_category`, `state`, 
        # `l3_integration`, `created_at`, [`liquidator`] `liquidator`, [`timestamp`] `timestamp`.
        # Example: https://starkscan.co/event/0x0774bebd15505d3f950c362d813dc81c6320ae92cb396b6469fd1ac5d8ff62dc_8.
        loan_id = int(event["data"][0], base = 16)
        user = event["data"][1]
        assert self.loan_entities[loan_id].user == user
        debt_token = src.constants.get_symbol(event["data"][2])
        # This prevents liquidated loans to appear as not repaid.
        debt_face_amount = decimal.Decimal("0")
        borrowed_collateral_token = src.constants.get_symbol(event["data"][6])
        borrowed_collateral_face_amount = decimal.Decimal(str(int(event["data"][7], base=16)))
        # Based on the documentation, it seems that it's only possible to liquidate the whole amount.
        assert borrowed_collateral_face_amount == decimal.Decimal("0")
        debt_category = int(event["data"][10], base=16)

        borrowed_collateral = src.state.TokenAmounts()
        borrowed_collateral.token_amounts[borrowed_collateral_token] = borrowed_collateral_face_amount
        self.loan_entities[loan_id].borrowed_collateral = borrowed_collateral
        # TODO: What happens to original collateral? For now, let's assume it disappears.
        original_collateral = src.state.TokenAmounts()
        self.loan_entities[loan_id].original_collateral = original_collateral
        self.loan_entities[loan_id].collateral.token_amounts = {
            token: (
                self.loan_entities[loan_id].original_collateral.token_amounts[token]
                + self.loan_entities[loan_id].borrowed_collateral.token_amounts[token]
            )
            for token in src.constants.TOKEN_DECIMAL_FACTORS
        }
        debt = src.state.TokenAmounts()
        debt.token_amounts[debt_token] = debt_face_amount
        self.loan_entities[loan_id].debt = debt
        self.loan_entities[loan_id].debt_category = debt_category
        if self.loan_entities[loan_id].user == self.verbose_user:
            logging.info(
                'In block number = {}, loan was liquidated, resulting in debt of face amount = {} of token = {}, '
                'borrowed collateral of face amount = {} of token = {} and no original collateral.'.format(
                    event["block_number"],
                    debt_face_amount,
                    debt_token,
                    borrowed_collateral_face_amount,
                    borrowed_collateral_token,
                )
            )

    def compute_liquidable_debt_at_price(
        self,
        prices: Dict[str, decimal.Decimal],
        collateral_token: str,
        collateral_token_price: decimal.Decimal,
        debt_token: str,
    ) -> decimal.Decimal:
        changed_prices = copy.deepcopy(prices)
        changed_prices[collateral_token] = collateral_token_price
        max_liquidated_amount = decimal.Decimal("0")
        for _, loan_entity in self.loan_entities.items():
            # Filter out users who borrowed the token of interest.
            debt_tokens = {
                token
                for token, token_amount in loan_entity.debt.token_amounts.items()
                if token_amount > decimal.Decimal("0")
            }
            if not debt_token in debt_tokens:
                continue

            # Filter out users with health factor below 1.
            debt_usd = loan_entity.compute_debt_usd(prices=changed_prices)
            health_factor = loan_entity.compute_health_factor(prices=changed_prices, debt_usd=debt_usd)
            health_factor_liquidation_threshold = (
                decimal.Decimal("1.06")
                if loan_entity.debt_category == 1
                else decimal.Decimal("1.05")
                if loan_entity.debt_category == 2
                else decimal.Decimal("1.04")
            )
            if health_factor >= health_factor_liquidation_threshold:
                continue

            # Find out how much of the `debt_token` will be liquidated.
            max_liquidated_amount += loan_entity.compute_debt_to_be_liquidated(debt_usd=debt_usd)
        return max_liquidated_amount

    def compute_number_of_active_users(self) -> int:
        unique_active_users = {
            loan_entity.user
            for loan_entity in self.loan_entities.values() 
            if loan_entity.has_collateral() or loan_entity.has_debt()
        }
        return len(unique_active_users)

    def compute_number_of_active_borrowers(self) -> int:
        unique_active_borrowers = {
            loan_entity.user
            for loan_entity in self.loan_entities.values() 
            if loan_entity.has_debt()
        }
        return len(unique_active_borrowers)