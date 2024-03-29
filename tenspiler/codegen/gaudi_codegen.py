import textwrap
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from metalift.ir import (
    Add,
    Call,
    Div,
    Eq,
    Expr,
    FnDecl,
    FnDeclRecursive,
    Ge,
    Gt,
    Int,
    Le,
    Lit,
    Lt,
    Matrix,
    Mul,
    ObjectT,
    Sub,
    Var,
    call,
    create_object,
    is_list_type,
    is_matrix_type,
)
from tenspiler.codegen.utils import DataType

# Indentation is 4 spaces
INDENTATION = " " * 4


class CType(Enum):
    """C types"""

    UINT8 = "uint8_t"
    FLOAT = "float"

    @property
    def is_primitive(self) -> bool:
        return True


class GaudiBodyType(CType, Enum):
    """Gaudi types used in the body (inside the loops). All C types are supported in the Gaudi body."""

    # 1 vector, with 256 8-bit integers
    UCHAR256 = "uchar256"
    # 1 vector, with 64 32-bit floats
    FLOAT64 = "float64"
    # 4 vectors, each with 64 32-bit integers
    UINT256 = "uint256"
    # 4 vectors, each with 64 32-bit floats
    FLOAT256 = "float256"

    @property
    def is_primitive(self) -> bool:
        return self in {GaudiBodyType.UINT8, GaudiBodyType.FLOAT}

    @staticmethod
    def from_ir_and_data_type(ir_type: ObjectT, d_type: DataType) -> "GaudiBodyType":
        if ir_type is Int:
            if d_type == DataType.INT:
                return GaudiBodyType.UINT8
            else:
                return GaudiBodyType.FLOAT
        elif is_list_type(ir_type) or is_matrix_type(ir_type):
            if d_type == DataType.INT:
                return GaudiBodyType.UCHAR256
            else:
                return GaudiBodyType.FLOAT64
        else:
            raise Exception(
                f"Unsupported Gaudi type for ir type {ir_type} and data type {d_type}"
            )


class GaudiHeaderType(CType, Enum):
    """Types used Gaudi's function header."""

    TENSOR = "tensor"

    @staticmethod
    def from_ir_and_data_type(ir_type: ObjectT, d_type: DataType) -> "GaudiHeaderType":
        if is_list_type(ir_type) or is_matrix_type(ir_type):
            return GaudiHeaderType.TENSOR
        elif ir_type is Int:
            if d_type == DataType.FLOAT:
                return GaudiHeaderType.FLOAT
            else:
                return GaudiHeaderType.UINT8
        else:
            raise Exception(
                f"Cannot infer Gaudi type from ir type {ir_type} and data type {d_type}"
            )


@dataclass
class GaudiInstr:
    dest_name: Optional[str]
    dest_type: Optional[GaudiBodyType]
    instr_str: str


def gaudi_codegen(
    ps_fn_decl: Union[FnDecl, FnDeclRecursive],
    all_synthesized_fns: Dict[str, Union[FnDecl, FnDeclRecursive]],
    override_arg_types: Dict[str, GaudiHeaderType] = {},
    d_type: DataType = DataType.INT,
) -> str:
    def expr_codegen(
        expr: Expr,
        instructions: List[GaudiInstr],
        *,
        override_type: Optional[GaudiBodyType] = None,  # Type to override
        vars_to_replace: Dict[str, Expr] = {},
    ) -> None:
        # Helper functions
        def get_gaudi_body_type(ir_type: ObjectT) -> GaudiBodyType:
            """Given the IR type, and the data type, returns the Gaudi type used in the body. Namely, when data type is float, all integers are converted to float."""
            if override_type is not None:
                return override_type
            else:
                return GaudiBodyType.from_ir_and_data_type(ir_type, d_type)

        def format_instruction(expr: Any, gaudi_body_type: GaudiBodyType) -> str:
            """Formats the instruction with the destination name and type."""
            return f"{gaudi_body_type.value} v{len(instructions)} = {expr};"

        def convert_arg(
            arg_name: str,
            arg_gaudi_type: GaudiBodyType,
            expected_gaudi_type: GaudiBodyType,
        ) -> GaudiInstr:
            """Converts the argument to the expected type. Returns the metadata of the new argument. Additionally, adds the instruction to the list of instructions."""
            non_default_switches = {
                (GaudiBodyType.UINT256, GaudiBodyType.FLOAT256): "SW_LINEAR",
                (GaudiBodyType.FLOAT256, GaudiBodyType.UCHAR256): "SW_RD",
            }
            convert_instr_name = (
                f"convert_{arg_gaudi_type.value}_to_{expected_gaudi_type.value}"
            )
            # By default, we use the 0 switch
            switch = non_default_switches.get((arg_gaudi_type, expected_gaudi_type), 0)
            convert_instr = format_instruction(
                f"{convert_instr_name}({arg_name}, {switch})", expected_gaudi_type
            )
            new_arg_name = f"v{len(instructions)}"
            new_metadata = GaudiInstr(
                dest_name=new_arg_name,
                dest_type=expected_gaudi_type,
                instr_str=convert_instr,
            )
            instructions.append(new_metadata)
            return new_metadata

        # Generate the instructions for the body
        if isinstance(expr, Var):
            if expr.name() in vars_to_replace:
                return expr_codegen(
                    vars_to_replace[expr.name()],
                    instructions,
                    vars_to_replace=vars_to_replace,
                )
            if is_list_type(expr.type) or is_matrix_type(expr.type):
                if d_type == DataType.FLOAT:
                    instr_name = f"v_f32_ld_tnsr_b"
                    instr_gaudi_type = GaudiBodyType.FLOAT64
                else:
                    instr_name = f"v_u8_ld_tnsr_b"
                    instr_gaudi_type = GaudiBodyType.UCHAR256
                instr = GaudiInstr(
                    dest_name=f"v{len(instructions)}",
                    dest_type=instr_gaudi_type,
                    instr_str=format_instruction(
                        f"{instr_name}(inputCoord, {expr.name()})", instr_gaudi_type
                    ),
                )
                instructions.append(instr)
                return
            else:
                instr_gaudi_type = get_gaudi_body_type(expr.type)
                instr = GaudiInstr(
                    dest_name=f"v{len(instructions)}",
                    dest_type=instr_gaudi_type,
                    instr_str=format_instruction(expr.name(), instr_gaudi_type),
                )
                instructions.append(instr)
                return
        elif isinstance(expr, Lit):
            instr_gaudi_type = get_gaudi_body_type(expr.type)
            instr = GaudiInstr(
                dest_name=f"v{len(instructions)}",
                dest_type=instr_gaudi_type,
                instr_str=format_instruction(expr.val(), instr_gaudi_type),
            )
            instructions.append(instr)
            return
        elif any(isinstance(expr, cls) for cls in [Add, Sub, Mul, Div]):
            instr_gaudi_type = get_gaudi_body_type(expr.type)
            if instr_gaudi_type.is_primitive:
                if isinstance(expr, Add):
                    op = "+"
                elif isinstance(expr, Sub):
                    op = "-"
                elif isinstance(expr, Mul):
                    op = "*"
                else:
                    op = "/"
                expr_codegen(
                    expr.args[0],
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                first_arg_metadata = instructions[-1]
                expr_codegen(
                    expr.args[1],
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                second_arg_metadata = instructions[-1]
                instr = GaudiInstr(
                    dest_name=f"v{len(instructions)}",
                    dest_type=instr_gaudi_type,
                    instr_str=format_instruction(
                        f"{first_arg_metadata.dest_name} {op} {second_arg_metadata.dest_name}",
                        instr_gaudi_type,
                    ),
                )
                instructions.append(instr)
                return
            else:
                if isinstance(expr, Add):
                    fn_name = "matrix_elemwise_add"
                elif isinstance(expr, Sub):
                    fn_name = "matrix_elemwise_sub"
                elif isinstance(expr, Mul):
                    fn_name = "matrix_elemwise_mul"
                else:
                    fn_name = "matrix_elemwise_div"
                expr_codegen(
                    call(fn_name, Matrix[Int], *expr.args).src,
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                return

        elif isinstance(expr, Call):
            fn_name = expr.name()

            # Handle select function
            if fn_name.endswith("matrix_selection_two_args"):
                for name, fn in all_synthesized_fns.items():
                    if name.endswith("select_two_args"):
                        select_two_args_fn_decl = fn
                if select_two_args_fn_decl is None:
                    raise ValueError("select_two_args not found")
                select_two_args_body = select_two_args_fn_decl.body()
                cond = select_two_args_body.c()
                if_then = select_two_args_body.e1()
                if_else = select_two_args_body.e2()
                select_args = select_two_args_fn_decl.arguments()[:2]
                matrix_args = expr.arguments()[:2]
                vars_to_replace: Dict[str, Expr] = {}
                for i in range(2):
                    vars_to_replace[select_args[i].name()] = matrix_args[i]
                if isinstance(cond, Gt):
                    if d_type == DataType.INT:
                        cond_instr_name = "v_u8_sel_grt_u8_b"
                    else:
                        cond_instr_name = "v_f32_sel_grt_f32_b"
                elif isinstance(cond, Eq):
                    if d_type == DataType.INT:
                        cond_instr_name = "v_u8_sel_eq_u8_b"
                    else:
                        cond_instr_name = "v_f32_sel_eq_f32_b"
                elif isinstance(cond, Lt):
                    if d_type == DataType.INT:
                        cond_instr_name = "v_u8_sel_less_u8_b"
                    else:
                        cond_instr_name = "v_f32_sel_less_f32_b"
                elif isinstance(cond, Le):
                    if d_type == DataType.INT:
                        cond_instr_name = "v_u8_sel_leq_u8_b"
                    else:
                        cond_instr_name = "v_f32_sel_leq_f32_b"
                elif isinstance(cond, Ge):
                    if d_type == DataType.INT:
                        cond_instr_name = "v_u8_sel_geq_u8_b"
                    else:
                        cond_instr_name = "v_f32_sel_geq_f32_b"
                else:
                    raise Exception(f"Unsupported condition {cond} for select_two_args")

                if d_type == DataType.INT:
                    instr_gaudi_type = GaudiBodyType.UCHAR256
                else:
                    instr_gaudi_type = GaudiBodyType.FLOAT64

                expr_codegen(
                    cond.args[0],
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                cond_arg0_metadata = instructions[-1]
                expr_codegen(
                    cond.args[1],
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                cond_arg1_metadata = instructions[-1]
                expr_codegen(
                    if_then,
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                if_then_metadata = instructions[-1]
                expr_codegen(
                    if_else,
                    instructions,
                    override_type=instr_gaudi_type,
                    vars_to_replace=vars_to_replace,
                )
                if_else_metadata = instructions[-1]

                select_instr_str = format_instruction(
                    f"{cond_instr_name}({cond_arg0_metadata.dest_name}, {cond_arg1_metadata.dest_name}, {if_then_metadata.dest_name}, {if_else_metadata.dest_name})",
                    instr_gaudi_type,
                )
                select_instr = GaudiInstr(
                    dest_name=f"v{len(instructions)}",
                    dest_type=instr_gaudi_type,
                    instr_str=select_instr_str,
                )
                instructions.append(select_instr)
                return

            # Group function names
            add_fn_names = {
                "matrix_elemwise_add",
                "vec_elemwise_add",
                "matrix_scalar_add",
                "vec_scalar_add",
            }
            sub_fn_names = {
                "matrix_elemwise_sub",
                "vec_elemwise_sub",
                "matrix_scalar_sub",
                "vec_scalar_sub",
                "scalar_matrix_sub",
                "scalar_vec_sub",
            }
            mul_fn_names = {
                "matrix_elemwise_mul",
                "vec_elemwise_mul",
                "matrix_scalar_mul",
                "vec_scalar_mul",
            }
            div_fn_names = {
                "matrix_elemwise_div",
                "vec_elemwise_div",
                "matrix_scalar_div",
                "vec_scalar_div",
                "scalar_matrix_div",
                "scalar_vec_div",
            }
            if fn_name in div_fn_names:
                if d_type == DataType.INT:
                    # Since we need to convert everything to float anyways, we just broadcast
                    # the scalar as a float from the beginning
                    expr_codegen(
                        expr.arguments()[0],
                        instructions,
                        override_type=GaudiBodyType.FLOAT64,
                        vars_to_replace=vars_to_replace,
                    )
                    first_arg_metadata = instructions[-1]
                    expr_codegen(
                        expr.arguments()[1],
                        instructions,
                        override_type=GaudiBodyType.FLOAT64,
                        vars_to_replace=vars_to_replace,
                    )
                    second_arg_metadata = instructions[-1]

                    # We need to convert all the uchar256/uint256 to float256
                    # If they are of type float64, we don't need to convert them, instead during multiplication
                    # we don't use the v1, v2, v3, v4 fields
                    if first_arg_metadata.dest_type not in {
                        GaudiBodyType.FLOAT256,
                        GaudiBodyType.FLOAT64,
                    }:
                        first_arg_metadata = convert_arg(
                            first_arg_metadata.dest_name,
                            first_arg_metadata.dest_type,
                            GaudiBodyType.FLOAT256,
                        )
                    if second_arg_metadata.dest_type not in {
                        GaudiBodyType.FLOAT256,
                        GaudiBodyType.FLOAT64,
                    }:
                        second_arg_metadata = convert_arg(
                            second_arg_metadata.dest_name,
                            second_arg_metadata.dest_type,
                            GaudiBodyType.FLOAT256,
                        )

                    # Now get fields to multiply
                    if first_arg_metadata.dest_type == GaudiBodyType.FLOAT64:
                        first_arg_mult_lst = [first_arg_metadata.dest_name]
                    else:
                        first_arg_mult_lst = [
                            f"{first_arg_metadata.dest_name}.v{i}" for i in range(1, 5)
                        ]

                    # Now get the reciprocal of the second argument
                    if second_arg_metadata.dest_type == GaudiBodyType.FLOAT64:
                        reciprocal_second_arg_name = f"v{len(instructions)}"
                        reciprocal_instr = GaudiInstr(
                            dest_name=reciprocal_second_arg_name,
                            dest_type=GaudiBodyType.FLOAT64,
                            instr_str=format_instruction(
                                f"v_reciprocal_fast_f32({second_arg_metadata.dest_name})",
                                GaudiBodyType.FLOAT64,
                            ),
                        )
                        instructions.append(reciprocal_instr)
                        second_arg_mult_lst = [reciprocal_second_arg_name]
                    else:
                        # second arg is of type float256. We need to call v_reciprocal_fast_f32
                        # on each of the fields.
                        second_arg_mult_lst = [
                            f"v_reciprocal_fast_f32({second_arg_metadata.dest_name}.v{i})"
                            for i in range(1, 5)
                        ]

                    # Now we both args are of type float256 or float65.
                    result_arg_name = f"v{len(instructions)}"
                    declaration_instr_metadata = GaudiInstr(
                        dest_name=result_arg_name,
                        dest_type=GaudiBodyType.FLOAT256,
                        instr_str=f"{GaudiBodyType.FLOAT256.value} {result_arg_name};",
                    )
                    instructions.append(declaration_instr_metadata)

                    for i in range(1, 5):
                        if len(first_arg_mult_lst) == 1:
                            first_arg_mult = first_arg_mult_lst[0]
                        else:
                            first_arg_mult = first_arg_mult_lst[i - 1]

                        if len(second_arg_mult_lst) == 1:
                            second_arg_mult = second_arg_mult_lst[0]
                        else:
                            second_arg_mult = second_arg_mult_lst[i - 1]

                        instr = GaudiInstr(
                            dest_name=None,
                            dest_type=None,
                            instr_str=f"{result_arg_name}.v{i} = v_f32_mul_b({first_arg_mult}, {second_arg_mult});",
                        )
                        instructions.append(instr)
                    # Last, we convert this float256 to uchar256
                    convert_arg(
                        result_arg_name, GaudiBodyType.FLOAT256, GaudiBodyType.UCHAR256
                    )
                    return
                else:
                    # Data type is float.
                    expr_codegen(
                        expr.arguments()[0],
                        instructions,
                        override_type=GaudiBodyType.FLOAT64,
                        vars_to_replace=vars_to_replace,
                    )
                    first_arg_metadata = instructions[-1]
                    expr_codegen(
                        expr.arguments()[1],
                        instructions,
                        override_type=GaudiBodyType.FLOAT64,
                        vars_to_replace=vars_to_replace,
                    )
                    second_arg_metadata = instructions[-1]

                    if fn_name.startswith("scalar"):
                        first_arg_metadata, second_arg_metadata = (
                            second_arg_metadata,
                            first_arg_metadata,
                        )

                    # Convert arguments to the correct types
                    if first_arg_metadata.dest_type != GaudiBodyType.FLOAT64:
                        first_arg_metadata = convert_arg(
                            first_arg_metadata.dest_name,
                            first_arg_metadata.dest_type,
                            GaudiBodyType.FLOAT64,
                        )
                    if second_arg_metadata.dest_type != GaudiBodyType.FLOAT64:
                        second_arg_metadata = convert_arg(
                            second_arg_metadata.dest_name,
                            second_arg_metadata.dest_type,
                            GaudiBodyType.FLOAT64,
                        )

                    reciprocal_instr_name = "v_reciprocal_fast_f32"
                    reciprocal_instr = format_instruction(
                        f"{reciprocal_instr_name}({second_arg_metadata.dest_name})",
                        GaudiBodyType.FLOAT64,
                    )
                    reciprocal_arg_name = f"v{len(instructions)}"
                    reciprocal_instr_metadata = GaudiInstr(
                        dest_name=reciprocal_arg_name,
                        dest_type=GaudiBodyType.FLOAT64,
                        instr_str=reciprocal_instr,
                    )
                    instructions.append(reciprocal_instr_metadata)

                    # Now we multiply the first arg by the reciprocal of the second arg
                    reciprocal_mul_instr = GaudiInstr(
                        dest_name=f"v{len(instructions)}",
                        dest_type=GaudiBodyType.FLOAT64,
                        instr_str=format_instruction(
                            f"v_f32_mul_b({first_arg_metadata.dest_name}, {reciprocal_arg_name})",
                            GaudiBodyType.FLOAT64,
                        ),
                    )
                    instructions.append(reciprocal_mul_instr)
                    return

            if fn_name in {*add_fn_names, *sub_fn_names, *mul_fn_names}:
                if d_type == DataType.INT:
                    expected_arg_type = GaudiBodyType.UCHAR256
                    if fn_name in add_fn_names:
                        instr_name = f"v_u8_add_b"
                        ret_gaudi_type = GaudiBodyType.UCHAR256
                    elif fn_name in sub_fn_names:
                        instr_name = f"v_u8_sub_b"
                        ret_gaudi_type = GaudiBodyType.UCHAR256
                    else:
                        instr_name = f"v_u8_mul_b"
                        ret_gaudi_type = GaudiBodyType.UINT256
                else:
                    expected_arg_type = GaudiBodyType.FLOAT64
                    if fn_name in add_fn_names:
                        instr_name = f"v_f32_add_b"
                        ret_gaudi_type = GaudiBodyType.FLOAT64
                    elif fn_name in sub_fn_names:
                        instr_name = f"v_f32_sub_b"
                        ret_gaudi_type = GaudiBodyType.FLOAT64
                    else:
                        instr_name = f"v_f32_mul_b"
                        ret_gaudi_type = GaudiBodyType.FLOAT64

                expr_codegen(
                    expr.arguments()[0],
                    instructions,
                    override_type=expected_arg_type,
                    vars_to_replace=vars_to_replace,
                )
                first_arg_metadata = instructions[-1]
                expr_codegen(
                    expr.arguments()[1],
                    instructions,
                    override_type=expected_arg_type,
                    vars_to_replace=vars_to_replace,
                )
                second_arg_metadata = instructions[-1]

                if fn_name.startswith("scalar"):
                    first_arg_metadata, second_arg_metadata = (
                        second_arg_metadata,
                        first_arg_metadata,
                    )

                # TODO: move convert to end of this function
                if first_arg_metadata.dest_type != expected_arg_type:
                    first_arg_metadata = convert_arg(
                        first_arg_metadata.dest_name,
                        first_arg_metadata.dest_type,
                        expected_arg_type,
                    )
                if second_arg_metadata.dest_type != expected_arg_type:
                    second_arg_metadata = convert_arg(
                        second_arg_metadata.dest_name,
                        second_arg_metadata.dest_type,
                        expected_arg_type,
                    )

                instr = GaudiInstr(
                    dest_name=f"v{len(instructions)}",
                    dest_type=ret_gaudi_type,
                    instr_str=format_instruction(
                        f"{instr_name}({first_arg_metadata.dest_name}, {second_arg_metadata.dest_name})",
                        ret_gaudi_type,
                    ),
                )
                instructions.append(instr)
                return

    ###############################
    # Begins actual code generation
    ###############################

    # TPC-C only supports vec-vec/matrix-matrix element-wise or vec-scalar/matrix-scalar
    # operations, which means the final return value has to either be a vector or a matrix.
    is_return_type_vec = is_list_type(ps_fn_decl.returnT())
    is_return_type_matrix = is_matrix_type(ps_fn_decl.returnT())
    if not is_return_type_vec and not is_return_type_matrix:
        raise Exception("Can only return a tensor from a TPC-C function!")

    # First we generate the function header. We include the tensor to return in the arguments,
    # and it should always be the last argument.
    rv_name = f"{ps_fn_decl.name()}_rv"
    rv = create_object(ps_fn_decl.returnT(), rv_name).src
    arguments = [
        f"{GaudiHeaderType.from_ir_and_data_type(arg.type, d_type).value} {arg.name()}"
        for arg in [*ps_fn_decl.arguments(), rv]
    ]
    arguments_str = ", ".join(arguments)
    header = f"void main({arguments_str})"

    # If mode is float, then we operate on 64 elements at a time, else 256
    if d_type == DataType.FLOAT:
        vec_len = 64
        store_instr = "v_f32_st_tnsr"
    else:
        vec_len = 256
        store_instr = "v_u8_st_tnsr"

    # Generate the returned expression
    instructions: List[GaudiInstr] = []
    expr_codegen(ps_fn_decl.body(), instructions)
    instr_strs = [instr.instr_str for instr in instructions]

    # Assign the last variable to the return value
    ret_expr = instructions[-1].dest_name

    if is_return_type_vec and not is_return_type_matrix:
        joined_instr_str = "\n".join(
            [instr_strs[0]]
            + [
                textwrap.indent(instr_str, INDENTATION * 3)
                for instr_str in instr_strs[1:]
            ]
        )
        body = f"""
        int5 index_space_start = get_index_space_offset();
        int5 index_space_end = index_space_start + get_index_space_size();

        int5 inputCoord = {{ 0 }};
        int5 outputCoord = {{ 0 }};

        unsigned vec_len = {vec_len};

        #pragma loop_unroll(8)
        for(int i = index_space_start[0]; i < index_space_end[0]; i++) {{
            // index space mapping
            inputCoord[0] = outputCoord[0] = (i * vec_len);
            {joined_instr_str}
            {store_instr}(outputCoord, {rv_name}, {ret_expr});
        }}
        """
        body = textwrap.dedent(body)
    else:
        joined_instr_str = "\n".join(
            [instr_strs[0]]
            + [
                textwrap.indent(instr_str, INDENTATION * 4)
                for instr_str in instr_strs[1:]
            ]
        )
        # matrix return type
        body = f"""
        int5 index_space_start = get_index_space_offset();
        int5 index_space_end = index_space_start + get_index_space_size();

        int5 inputCoord = {{ 0 }};
        int5 outputCoord = {{ 0 }};

        unsigned vec_len = {vec_len};

        for(int i = index_space_start[0]; i < index_space_end[0]; i++) {{
            #pragma loop_unroll(4)
            for (int j = index_space_start[1]; j < index_space_end[1]; j++) {{
                // index space mapping
                // coordinate 0 is for dim0.
                inputCoord[0] = outputCoord[0] = (i * vec_len);
                // coordinate 1 is for dim1.
                inputCoord[1] = outputCoord[1] = j;

                {joined_instr_str}

                {store_instr}(outputCoord, {rv_name}, {ret_expr});
            }}
        }}
        """
        body = textwrap.dedent(body)

    header_and_body = f"""
    {header} {{
        {textwrap.indent(body, INDENTATION * 2)}
    }}
    """
    header_and_body = textwrap.dedent(header_and_body)

    return header_and_body
