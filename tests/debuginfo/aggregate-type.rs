//@ compile-flags:-g

// === LLDB TESTS ==================================================================================

//@ lldb-command:run
//@ lldb-repr:a
//@ lldb-repr:b

#![allow(unused_variables)]

struct A {
    f1: u8,
    f2: i16
}


fn main() {
    let a = A { f1: 5, f2: -5};
    let b = vec![0u32, 1, 2, 3, 4, 5];
    _zzz(); // #break
}

fn _zzz() {
    ()
}
