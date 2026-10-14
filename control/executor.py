"""Compatibility import: only the Terraform approval executor is active."""
from control.terraform import validate_action, apply as execute_approval, main
from control.store import store

if __name__ == '__main__':
    main()
